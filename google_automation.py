"""
Google One automation using undetected-chromedriver + device registration.
1. Login with undetected Chrome (bypasses most detection)
2. Extract OAuth token
3. Register Pixel 10 Pro device to account
4. Navigate Google One and find Gemini Pro offer
"""
import logging
import time
import re
import json
from urllib.parse import urlparse
from typing import Optional

import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import pyotp

import config
from device_simulator import DeviceProfile
from device_registration import add_pixel_device_to_account

logger = logging.getLogger(__name__)


def _build_driver(profile: DeviceProfile) -> uc.Chrome:
    """Return undetected Chrome with Pixel 10 Pro mobile emulation."""
    options = uc.ChromeOptions()

    if config.HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=390,844")
    options.add_argument(f"--user-agent={profile.user_agent}")

    mobile_emulation = {
        "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0},
        "userAgent": profile.user_agent,
    }
    options.add_experimental_option("mobileEmulation", mobile_emulation)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = uc.Chrome(options=options, version_main=124)
    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


def _wait_for(driver, by, value, timeout=config.WEBDRIVER_TIMEOUT):
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def _extract_oauth_token(driver) -> Optional[str]:
    """Extract OAuth token from logged-in Google session."""
    try:
        driver.get("https://accounts.google.com/ServiceLogin?continue=https://one.google.com")
        time.sleep(3)

        token = driver.execute_script("""
            for (var i = 0; i < localStorage.length; i++) {
                var key = localStorage.key(i);
                if (key.includes('oauth') || key.includes('token') || key.includes('auth')) {
                    var val = localStorage.getItem(key);
                    if (val && val.length > 50) return val;
                }
            }
            return null;
        """)

        if token and len(token) > 20:
            logger.info("OAuth token extracted")
            return token

        page_source = driver.page_source
        match = re.search(r'access_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', page_source)
        if match:
            return match.group(1)

        return None
    except Exception as e:
        logger.warning("Token error: %s", e)
        return None


def _gmail_login(driver, email: str, password: str, totp_key: str = "") -> bool:
    """Login to Google with undetected Chrome."""
    try:
        driver.get(config.GMAIL_LOGIN_URL)
        time.sleep(3)

        email_field = _wait_for(driver, By.CSS_SELECTOR, 'input[type="email"]')
        email_field.clear()
        email_field.send_keys(email)
        time.sleep(1)

        next_btn = _wait_for(driver, By.ID, "identifierNext")
        next_btn.click()
        time.sleep(4)

        password_field = _wait_for(driver, By.CSS_SELECTOR, 'input[type="password"]')
        password_field.clear()
        password_field.send_keys(password)
        time.sleep(1)

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()
        time.sleep(5)

        if totp_key:
            try:
                clean_key = totp_key.replace(" ", "").upper()
                code = pyotp.TOTP(clean_key).now()
                logger.info("TOTP: %s", code)
                time.sleep(3)

                for sel in ['input[type="tel"]', 'input[id*="totp"]', 'input[id*="code"]',
                           'input[autocomplete="one-time-code"]']:
                    try:
                        field = _wait_for(driver, By.CSS_SELECTOR, sel, timeout=5)
                        field.clear()
                        field.send_keys(code)
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
                logger.warning("TOTP error: %s", e)

        time.sleep(3)
        current_url = driver.current_url
        hostname = urlparse(current_url).hostname or ""

        for ok_host in ["myaccount.google.com", "one.google.com", "mail.google.com"]:
            if ok_host in hostname:
                logger.info("Login OK: %s", current_url)
                return True

        if "accounts.google.com" in hostname and "/signin" in urlparse(current_url).path:
            logger.warning("Still on signin page")
            return False

        logger.info("Login likely OK: %s", current_url)
        return True

    except (TimeoutException, WebDriverException) as e:
        logger.error("Login error: %s", e)
        return False


def _extract_payment_link(driver) -> Optional[str]:
    """Scan page for Gemini Pro offer link."""
    keywords = config.GEMINI_OFFER_KEYWORDS
    all_links = driver.find_elements(By.TAG_NAME, "a")

    for link in all_links:
        try:
            text = (link.text + " " + (link.get_attribute("aria-label") or "")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                return href
        except Exception:
            continue

    url_patterns = re.compile(r"(gemini|upgrade|activate|offer|redeem|trial|checkout)", re.IGNORECASE)
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if url_patterns.search(href):
                return href
        except Exception:
            continue

    return None


def _navigate_google_one(driver) -> Optional[str]:
    """Navigate Google One and find offer."""
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating: %s", url)
            driver.get(url)
            time.sleep(5)

            for selector in ('[aria-label="Accept all"]', 'button[jsname="higCR"]'):
                try:
                    driver.find_element(By.CSS_SELECTOR, selector).click()
                    time.sleep(1)
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link
        except Exception as e:
            logger.warning("Error at %s: %s", url, e)

    return None


class GoogleAutomationError(Exception):
    pass


def check_gemini_offer(email: str, password: str,
                       device: DeviceProfile,
                       totp_key: str = "") -> Optional[str]:
    """FULL AUTO: undetected Chrome login + device registration + offer find."""
    driver = None
    try:
        logger.info("Undetected Chrome (session: %s)", device.session_id)
        driver = _build_driver(device)

        if not _gmail_login(driver, email, password, totp_key):
            raise GoogleAutomationError("Login failed - check credentials")

        oauth_token = _extract_oauth_token(driver)
        if oauth_token:
            logger.info("Registering Pixel device...")
            add_pixel_device_to_account(email, oauth_token)
            time.sleep(3)

        offer_link = _navigate_google_one(driver)
        time.sleep(30 if offer_link else 60)
        return offer_link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
