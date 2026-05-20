"""
Google One automation - simple Selenium + CDP stealth.
Login -> Google One -> find Gemini Pro offer.
"""
import logging, time, re
from urllib.parse import urlparse
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import pyotp

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


def _build_driver(profile: DeviceProfile) -> webdriver.Chrome:
    options = Options()
    if config.HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=390,844")
    options.add_argument(f"--user-agent={profile.user_agent}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("mobileEmulation", {
        "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0},
        "userAgent": profile.user_agent,
    })

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        window.chrome = {runtime: {}};
    """})

    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


def _wait(driver, by, value, timeout=config.WEBDRIVER_TIMEOUT):
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))


def _do_login(driver, email, password, totp_key=""):
    driver.get(config.GMAIL_LOGIN_URL)
    time.sleep(3)

    _wait(driver, By.CSS_SELECTOR, 'input[type="email"]').send_keys(email)
    _wait(driver, By.ID, "identifierNext").click()
    time.sleep(4)

    _wait(driver, By.CSS_SELECTOR, 'input[type="password"]').send_keys(password)
    _wait(driver, By.ID, "passwordNext").click()
    time.sleep(5)

    if totp_key:
        try:
            code = pyotp.TOTP(totp_key.replace(" ", "").upper()).now()
            logger.info("TOTP: %s", code)
            time.sleep(3)

            # --- Resilient 2FA "Try Another Way" Switching ---
            # If Google defaults to sending phone prompt notification and there is no direct input field,
            # we need to click "Try another way" (Другой способ) and select the Authenticator code option.
            input_found = False
            for sel in ['input[type="tel"]', 'input[id*="totp"]', 'input[id*="code"]',
                       'input[autocomplete="one-time-code"]']:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    input_found = True
                    break

            if not input_found:
                logger.info("No direct TOTP input field found. Attempting to switch verification method...")
                # 1. Search and click "Try another way" / "Другой способ" / "Другие способы"
                way_clicked = False
                for btn_text in ["Try another way", "Другой способ", "Другие способы"]:
                    try:
                        btn = driver.find_element(By.XPATH, f"//*[contains(text(), '{btn_text}')]")
                        if btn.is_displayed():
                            btn.click()
                            logger.info("Clicked '%s' link", btn_text)
                            time.sleep(3)
                            way_clicked = True
                            break
                    except NoSuchElementException:
                        continue

                # 2. Select Authenticator option in the menu
                if way_clicked:
                    for opt_text in ["Authenticator", "приложения Google Authenticator", "приложения"]:
                        try:
                            opt = driver.find_element(By.XPATH, f"//*[contains(text(), '{opt_text}')]")
                            opt.click()
                            logger.info("Selected '%s' option from 2FA list", opt_text)
                            time.sleep(4)
                            break
                        except NoSuchElementException:
                            continue

            # Standard TOTP entering loop
            for sel in ['input[type="tel"]', 'input[id*="totp"]', 'input[id*="code"]',
                       'input[autocomplete="one-time-code"]']:
                try:
                    f = _wait(driver, By.CSS_SELECTOR, sel, timeout=5)
                    f.send_keys(code)
                    time.sleep(1)
                    try:
                        driver.find_element(By.ID, "totpNext").click()
                    except Exception:
                        driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
                    time.sleep(4)
                    break
                except TimeoutException:
                    continue
        except Exception as e:
            logger.warning("TOTP: %s", e)

    time.sleep(3)
    hostname = urlparse(driver.current_url).hostname or ""
    if any(h in hostname for h in ["myaccount.google.com", "one.google.com", "mail.google.com"]):
        return True
    if "accounts.google.com" in hostname and "/signin" in urlparse(driver.current_url).path:
        return False
    return True


def _find_offer_link(driver):
    keywords = config.GEMINI_OFFER_KEYWORDS
    for link in driver.find_elements(By.TAG_NAME, "a"):
        try:
            text = (link.text + " " + (link.get_attribute("aria-label") or "")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                return href
        except Exception:
            continue
    pat = re.compile(r"(gemini|upgrade|activate|offer|redeem|trial|checkout)", re.IGNORECASE)
    for link in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = link.get_attribute("href") or ""
            if pat.search(href):
                return href
        except Exception:
            continue
    return None


TRIAL_KEYWORDS = [
    # English
    "try for free", "start trial", "get started", "try free",
    "claim", "activate", "redeem", "get offer", "start free",
    "free trial", "get gemini", "upgrade", "try gemini",
    "get 12 month", "get 1 year",
    # Russian
    "попробовать бесплатно", "начать пробный", "получить предложение",
    "активировать", "получить бесплатно", "попробовать", "начать",
    "бесплатно", "получить",
]


OFFER_URL_PATTERNS = [
    "payments.google.com",
    "play.google.com/store/account",
    "one.google.com/checkout",
    "one.google.com/offers",
    "store.google.com",
]


def _find_checkout_url_after_clicks(driver) -> Optional[str]:
    # 1. Check if the current URL is already a checkout page
    cur_url = driver.current_url
    if any(pat in cur_url for pat in OFFER_URL_PATTERNS):
        logger.info("Current URL is already a checkout page: %s", cur_url)
        return cur_url

    # 2. Try the static anchor check first
    link = _find_offer_link(driver)
    if link:
        logger.info("Found direct offer link via static search: %s", link)
        return link

    # 3. Find potential trial buttons or links that might launch the flow
    selectors = [
        "button", 
        "a", 
        "[role='button']", 
        "div[class*='btn']", 
        "div[class*='button']",
        "span[class*='btn']",
        "span[class*='button']"
    ]
    
    candidates = []
    seen = set()
    
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in elements:
                try:
                    if el in seen:
                        continue
                    if not el.is_displayed():
                        continue
                    text = (el.text or "").lower()
                    aria_label = (el.get_attribute("aria-label") or "").lower()
                    combined = text + " " + aria_label
                    if any(kw in combined for kw in TRIAL_KEYWORDS):
                        candidates.append(el)
                        seen.add(el)
                except Exception:
                    continue
        except Exception:
            continue

    logger.info("Found %d candidate offer buttons/links", len(candidates))
    if not candidates:
        return None

    original_handle = driver.current_window_handle

    for i, el in enumerate(candidates):
        try:
            text = (el.text or el.get_attribute("aria-label") or "Element").strip()
            logger.info("Clicking candidate %d/%d: '%s'", i+1, len(candidates), text)
            
            pre_handles = driver.window_handles
            
            # Click candidate
            try:
                el.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", el)
                except Exception as click_err:
                    logger.warning("Failed to click candidate %d: %s", i+1, click_err)
                    continue

            # Wait for redirect/navigation or modal to load
            time.sleep(6)

            # Check if any new tab/window was opened
            post_handles = driver.window_handles
            if len(post_handles) > len(pre_handles):
                logger.info("New tab/window opened after clicking '%s'", text)
                for handle in post_handles:
                    if handle != original_handle:
                        try:
                            driver.switch_to.window(handle)
                            time.sleep(2)
                            new_url = driver.current_url
                            logger.info("New tab URL: %s", new_url)
                            if any(pat in new_url for pat in OFFER_URL_PATTERNS):
                                logger.info("Captured checkout link from new window: %s", new_url)
                                # Keep the driver clean - close new window
                                driver.close()
                                driver.switch_to.window(original_handle)
                                return new_url
                            driver.close()
                        except Exception as w_err:
                            logger.warning("Error inspecting new tab: %s", w_err)
                driver.switch_to.window(original_handle)

            # Check if current URL of original window changed
            updated_url = driver.current_url
            if any(pat in updated_url for pat in OFFER_URL_PATTERNS):
                logger.info("Captured checkout link from main window redirect: %s", updated_url)
                return updated_url

            # -- UCP / IFrame Widget Verification --
            # 1. Inspect iframe source URLs directly
            try:
                for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                    try:
                        src = iframe.get_attribute("src") or ""
                        if any(pat in src for pat in OFFER_URL_PATTERNS):
                            logger.info("Captured checkout link from iframe src: %s", src)
                            return src
                    except Exception:
                        continue
            except Exception:
                pass

            # 2. Switch inside every iframe and scan contents
            try:
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for iframe in iframes:
                    try:
                        driver.switch_to.frame(iframe)
                        for a_link in driver.find_elements(By.TAG_NAME, "a"):
                            try:
                                href = a_link.get_attribute("href") or ""
                                if any(pat in href for pat in OFFER_URL_PATTERNS):
                                    logger.info("Captured checkout link inside iframe: %s", href)
                                    driver.switch_to.default_content()
                                    return href
                            except Exception:
                                continue
                        driver.switch_to.default_content()
                    except Exception:
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
            except Exception:
                pass

            # Check if interactive frame or new checkout links appeared on current page
            for a_link in driver.find_elements(By.TAG_NAME, "a"):
                try:
                    href = a_link.get_attribute("href") or ""
                    if any(pat in href for pat in OFFER_URL_PATTERNS):
                        logger.info("Captured checkout link from page after click: %s", href)
                        return href
                except Exception:
                    continue

        except Exception as e:
            logger.warning("Error processing candidate %d: %s", i+1, e)
            try:
                driver.switch_to.window(original_handle)
            except Exception:
                pass
            continue

    return None


def _check_google_one(driver):
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(5)
            for s in ('[aria-label="Accept all"]', 'button[jsname="higCR"]'):
                try:
                    driver.find_element(By.CSS_SELECTOR, s).click()
                    time.sleep(1)
                except NoSuchElementException:
                    pass
            
            link = _find_checkout_url_after_clicks(driver)
            if link:
                return link
        except Exception as e:
            logger.warning("Nav %s: %s", url, e)
    return None


class GoogleAutomationError(Exception):
    pass


def check_gemini_offer(email: str, password: str, device: DeviceProfile,
                       totp_key: str = "") -> Optional[str]:
    """
    Login + find Gemini Pro offer. Runs with 90s timeout.
    """
    driver = None
    try:
        driver = _build_driver(device)

        if not _do_login(driver, email, password, totp_key):
            raise GoogleAutomationError("Login failed - check credentials")

        logger.info("Logged in, searching Google One...")
        link = _check_google_one(driver)
        return link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
