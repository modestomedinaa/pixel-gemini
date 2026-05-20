"""
Google One automation using Selenium.

Logs into a Gmail account, navigates to Google One, detects the
12-month free Gemini Pro offer, and returns the activation / payment link.
"""

import logging
import time
import re
from urllib.parse import urlparse
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import pyotp

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Driver factory ────────────────────────────────────────────────────────────

def _build_driver(profile: DeviceProfile) -> webdriver.Chrome:
    """Return a headless Chrome WebDriver configured for the device profile."""
    options = Options()

    if config.HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=390,844")  # Pixel 10 Pro screen size
    options.add_argument(f"--user-agent={profile.user_agent}")

    # Mobile emulation – Pixel 10 Pro viewport
    mobile_emulation = {
        "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0},
        "userAgent": profile.user_agent,
    }
    options.add_experimental_option("mobileEmulation", mobile_emulation)

    # Suppress automation flags
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    service = Service()  # relies on chromedriver being on PATH (Replit provides it)
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


# ── Login helper ──────────────────────────────────────────────────────────────

def _wait_for(driver: webdriver.Chrome, by: str, value: str,
               timeout: int = config.WEBDRIVER_TIMEOUT) -> object:
    """Return element after waiting for it to be clickable."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def _check_captcha(driver: webdriver.Chrome) -> bool:
    """Detect if Google is showing a captcha. If yes, wait for manual solve."""
    captcha_indicators = [
        'captcha', 'CAPTCHA', 'verify', 'Verify',
        'unusual traffic', 'robot', 'automated',
        'type the characters', 'Type the text',
        'confirm you are not a robot',
    ]
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        for indicator in captcha_indicators:
            if indicator.lower() in body_text.lower():
                logger.warning("CAPTCHA detected! Indicator: %s", indicator)
                driver.save_screenshot("captcha_detected.png")
                logger.info("Waiting 120s for manual captcha solve...")
                time.sleep(120)
                driver.save_screenshot("captcha_after_wait.png")
                return True
        captcha_imgs = driver.find_elements(By.CSS_SELECTOR, 'img[src*="captcha"], img[alt*="captcha" i]')
        if captcha_imgs:
            logger.warning("CAPTCHA image found!")
            driver.save_screenshot("captcha_detected.png")
            logger.info("Waiting 120s for manual captcha solve...")
            time.sleep(120)
            driver.save_screenshot("captcha_after_wait.png")
            return True
    except Exception:
        pass
    return False


def _gmail_login(driver: webdriver.Chrome, email: str, password: str, totp_key: str = "") -> bool:
    """
    Perform Gmail / Google account login.

    Returns True on apparent success, False on detectable failure.
    """
    try:
        driver.get(config.GMAIL_LOGIN_URL)
        time.sleep(2)

        # ── Email step ────────────────────────────────────────────────────────
        email_field = _wait_for(driver, By.CSS_SELECTOR,
                                'input[type="email"]')
        email_field.clear()
        email_field.send_keys(email)
        driver.save_screenshot("debug_01_email.png")

        next_btn = _wait_for(driver, By.ID, "identifierNext")
        next_btn.click()
        time.sleep(3)
        _check_captcha(driver)
        driver.save_screenshot("debug_02_after_email.png")

        # ── Password step ─────────────────────────────────────────────────────
        password_field = _wait_for(driver, By.CSS_SELECTOR,
                                   'input[type="password"]')
        password_field.clear()
        password_field.send_keys(password)
        driver.save_screenshot("debug_03_password.png")

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()
        time.sleep(4)
        _check_captcha(driver)
        driver.save_screenshot("debug_04_after_password.png")

        # ── 2FA / TOTP step ──────────────────────────────────────────────────
        if totp_key:
            try:
                # Clean up the key (remove spaces)
                clean_key = totp_key.replace(" ", "").upper()
                totp = pyotp.TOTP(clean_key)
                code = totp.now()
                logger.info("Generated TOTP code: %s", code)

                time.sleep(3)
                _check_captcha(driver)
                driver.save_screenshot("debug_05_2fa_page.png")

                # First, check if Google is showing phone prompt ("Verify it's you")
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    if "tap yes" in body_text or "phone" in body_text or "notification" in body_text:
                        logger.info("Phone verification prompt detected, clicking 'Try another way'...")
                        # Click "Try another way" / "More ways to verify"
                        for link_text in ["Try another way", "More ways to verify", "Another way"]:
                            try:
                                elem = driver.find_element(By.XPATH, f"//*[contains(text(), '{link_text}')]")
                                elem.click()
                                time.sleep(2)
                                logger.info("Clicked: %s", link_text)
                                break
                            except Exception:
                                continue
                        time.sleep(2)
                        driver.save_screenshot("debug_05b_after_try_another.png")

                        # Now select "Google Authenticator" or "Authenticator app"
                        for auth_text in ["Authenticator", "Google Authenticator", "authenticator app"]:
                            try:
                                elem = driver.find_element(By.XPATH, f"//*[contains(text(), '{auth_text}')]")
                                elem.click()
                                time.sleep(2)
                                logger.info("Selected: %s", auth_text)
                                break
                            except Exception:
                                continue
                        time.sleep(2)
                        driver.save_screenshot("debug_05c_authenticator_selected.png")
                except Exception as e:
                    logger.info("No phone prompt detected: %s", e)

                # Try multiple selectors for 2FA input
                totp_selectors = [
                    'input[type="tel"]',
                    'input[id*="totp"]',
                    'input[id*="code"]',
                    'input[aria-label*="code" i]',
                    'input[aria-label*="Enter" i]',
                    'input[autocomplete="one-time-code"]',
                ]
                totp_field = None
                for sel in totp_selectors:
                    try:
                        totp_field = _wait_for(driver, By.CSS_SELECTOR, sel, timeout=5)
                        logger.info("Found 2FA field: %s", sel)
                        break
                    except TimeoutException:
                        continue

                if totp_field:
                    totp_field.clear()
                    totp_field.send_keys(code)
                    driver.save_screenshot("debug_06_totp_entered.png")
                    time.sleep(1)

                    # Click Next/Verify
                    try:
                        totp_next = _wait_for(driver, By.ID, "totpNext", timeout=5)
                        totp_next.click()
                    except Exception:
                        try:
                            driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
                        except Exception:
                            driver.find_element(By.CSS_SELECTOR, 'button').click()
                    time.sleep(3)
                    _check_captcha(driver)
                    driver.save_screenshot("debug_07_after_totp.png")
                    logger.info("TOTP submitted")
                else:
                    logger.warning("No 2FA input field found on page")
                    # Check for other 2FA methods
                    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    logger.info("2FA page text: %s", page_text[:200])
            except Exception as exc:
                logger.warning("TOTP step error (continuing): %s", exc)
                driver.save_screenshot("debug_totp_error.png")

        # ── Verify login ──────────────────────────────────────────────────────
        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""
        if (
            hostname == "myaccount.google.com"
            or hostname.endswith(".google.com")
            and "/u/" in path
        ):
            logger.info("Login succeeded for %s", email)
            return True

        # Check for error messages
        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"]'
            )
            if error_el.text:
                logger.warning("Login error detected: %s", error_el.text)
                return False
        except NoSuchElementException:
            pass

        # If we're no longer on the login page, assume success
        if not (
            hostname == "accounts.google.com"
            and path.startswith("/signin")
        ):
            logger.info("Login appeared successful for %s (URL: %s)",
                        email, current_url)
            return True

        driver.save_screenshot("debug_login_failed.png")
        logger.warning("Unexpected URL after login: %s", current_url)
        # Log page text for debugging
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:300]
            logger.info("Page text: %s", body_text)
        except Exception:
            pass
        return False

    except TimeoutException as exc:
        logger.error("Timeout during login: %s", exc)
        return False
    except WebDriverException as exc:
        logger.error("WebDriver error during login: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────

def _extract_payment_link(driver: webdriver.Chrome) -> Optional[str]:
    """
    Scan the current page for a Gemini Pro offer / activation link.

    Strategy:
    1. Look for anchor tags whose text or aria-label contains offer keywords.
    2. Fall back to scanning all links for 'gemini' or 'upgrade' patterns.
    3. Return the first matching href found.
    """
    keywords = config.GEMINI_OFFER_KEYWORDS

    # -- Strategy 1: anchor text / aria-label match ---------------------------
    all_links = driver.find_elements(By.TAG_NAME, "a")
    for link in all_links:
        try:
            text = (link.text + " " + link.get_attribute("aria-label")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                logger.info("Found offer link via text match: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 2: URL pattern scan -----------------------------------------
    url_patterns = re.compile(
        r"(gemini|upgrade|activate|offer|redeem|trial|checkout)",
        re.IGNORECASE,
    )
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if url_patterns.search(href):
                logger.info("Found offer link via URL pattern: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 3: button / CTA elements ------------------------------------
    buttons = driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
    for btn in buttons:
        try:
            text = btn.text.lower()
            if any(kw in text for kw in keywords):
                # Try to find parent anchor
                try:
                    parent_link = btn.find_element(By.XPATH, "ancestor::a")
                    href = parent_link.get_attribute("href") or ""
                    if href:
                        logger.info("Found offer link via button parent: %s", href)
                        return href
                except NoSuchElementException:
                    pass
                # Return current URL as fallback (user will land on offer page)
                logger.info("Found offer CTA button on page: %s", driver.current_url)
                return driver.current_url
        except Exception:
            continue

    return None


def _navigate_google_one(driver: webdriver.Chrome) -> Optional[str]:
    """
    Navigate to Google One and attempt to find the Gemini Pro offer link.

    Returns the payment/activation URL or None if not found.
    """
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(3)

            # Dismiss cookie/consent banners if present
            for selector in (
                '[aria-label="Accept all"]',
                'button[jsname="higCR"]',
                '[data-action="accept"]',
            ):
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link

        except (TimeoutException, WebDriverException) as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""


def check_gemini_offer(email: str, password: str,
                       device: DeviceProfile,
                       totp_key: str = "") -> Optional[str]:
    """
    HYBRID MODE: Opens visible browser, user logs in manually,
    then bot auto-navigates Google One and extracts the offer link.

    The email/password/totp are NOT used for automated login -
    they're kept for session tracking only.
    """
    driver: Optional[webdriver.Chrome] = None
    try:
        logger.info("Starting WebDriver for session %s (HYBRID MODE)", device.session_id)
        driver = _build_driver(device)

        # Go directly to Google One - user will be prompted to log in
        logger.info("Opening Google One - please log in manually in the browser window")
        driver.get(config.GOOGLE_ONE_URL)
        driver.save_screenshot("manual_login_start.png")

        # Wait for manual login (up to 5 minutes)
        logger.info("Waiting up to 300s for manual login...")
        logged_in = False
        for i in range(60):
            time.sleep(5)
            try:
                current_url = driver.current_url
                parsed = urlparse(current_url)
                hostname = parsed.hostname or ""

                # Check if we're past the login page
                if hostname == "one.google.com" and "signin" not in current_url.lower():
                    # Verify we're actually logged in by checking for account content
                    try:
                        body = driver.find_element(By.TAG_NAME, "body").text.lower()
                        if "sign in" not in body and "login" not in body:
                            logged_in = True
                            logger.info("Manual login detected! URL: %s", current_url)
                            break
                    except Exception:
                        pass
                elif hostname == "myaccount.google.com":
                    logged_in = True
                    logger.info("Login detected via myaccount redirect")
                    # Navigate back to Google One
                    driver.get(config.GOOGLE_ONE_URL)
                    time.sleep(3)
                    break
            except Exception:
                pass
            if i % 6 == 0:
                logger.info("Still waiting for login... (%ds)", (i+1)*5)

        if not logged_in:
            driver.save_screenshot("manual_login_timeout.png")
            raise GoogleAutomationError(
                "Login timeout – please log in within 5 minutes."
            )

        driver.save_screenshot("logged_in.png")
        logger.info("User logged in! Searching for Gemini offer...")

        # Now find the offer
        offer_link = _navigate_google_one(driver)

        # Keep browser open for 60s so user can see result
        if offer_link:
            logger.info("Offer found, keeping browser open 60s...")
            time.sleep(60)
        else:
            logger.info("No offer found, keeping browser open 120s for manual check...")
            time.sleep(120)

        return offer_link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
