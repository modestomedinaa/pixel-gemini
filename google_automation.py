"""
Google One automation - Selenium + CDP stealth + device registration.
"""
import logging, time, re, json
from urllib.parse import urlparse
from typing import Optional

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
from device_registration import add_pixel_device_to_account

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

    # CDP stealth patches
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        window.chrome = {runtime: {}};
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) :
            originalQuery(parameters)
        );
    """})

    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


def _wait_for(driver, by, value, timeout=config.WEBDRIVER_TIMEOUT):
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))


def _extract_oauth_token(driver) -> Optional[str]:
    try:
        driver.get("https://one.google.com")
        time.sleep(3)
        token = driver.execute_script("""
            for (let i = 0; i < localStorage.length; i++) {
                let key = localStorage.key(i);
                if (key.includes('oauth') || key.includes('token') || key.includes('auth')) {
                    let val = localStorage.getItem(key);
                    if (val && val.length > 50) return val;
                }
            }
            return null;
        """)
        if token and len(token) > 20:
            return token
        match = re.search(r'access_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', driver.page_source)
        return match.group(1) if match else None
    except Exception as e:
        logger.warning("Token error: %s", e)
        return None


def _gmail_login(driver, email: str, password: str, totp_key: str = "") -> bool:
    try:
        driver.get(config.GMAIL_LOGIN_URL)
        time.sleep(3)

        _wait_for(driver, By.CSS_SELECTOR, 'input[type="email"]').send_keys(email)
        _wait_for(driver, By.ID, "identifierNext").click()
        time.sleep(4)

        _wait_for(driver, By.CSS_SELECTOR, 'input[type="password"]').send_keys(password)
        _wait_for(driver, By.ID, "passwordNext").click()
        time.sleep(5)

        if totp_key:
            try:
                code = pyotp.TOTP(totp_key.replace(" ", "").upper()).now()
                logger.info("TOTP: %s", code)
                time.sleep(3)
                for sel in ['input[type="tel"]', 'input[id*="totp"]', 'input[id*="code"]',
                           'input[autocomplete="one-time-code"]']:
                    try:
                        f = _wait_for(driver, By.CSS_SELECTOR, sel, timeout=5)
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
    except Exception as e:
        logger.error("Login: %s", e)
        return False


def _extract_payment_link(driver) -> Optional[str]:
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


def _navigate_google_one(driver) -> Optional[str]:
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            driver.get(url)
            time.sleep(5)
            for s in ('[aria-label="Accept all"]', 'button[jsname="higCR"]'):
                try:
                    driver.find_element(By.CSS_SELECTOR, s).click()
                    time.sleep(1)
                except NoSuchElementException:
                    pass
            link = _extract_payment_link(driver)
            if link:
                return link
        except Exception as e:
            logger.warning("Nav %s: %s", url, e)
    return None


class GoogleAutomationError(Exception):
    pass


def check_gemini_offer(email: str, password: str, device: DeviceProfile,
                       totp_key: str = "") -> Optional[str]:
    driver = None
    try:
        driver = _build_driver(device)
        if not _gmail_login(driver, email, password, totp_key):
            raise GoogleAutomationError("Login failed - check credentials")

        token = _extract_oauth_token(driver)
        if token:
            add_pixel_device_to_account(email, token)
            time.sleep(3)

        link = _navigate_google_one(driver)
        time.sleep(30 if link else 60)
        return link
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
