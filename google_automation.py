"""
Google One automation - simple Selenium + CDP stealth.
Login -> Google One -> find Gemini Pro offer.
"""
import logging, time, re, os
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


def _build_driver(profile: DeviceProfile, email: str) -> webdriver.Chrome:
    options = Options()
    if config.HEADLESS:
        options.add_argument("--headless=new")
    
    # Configure optional proxy routing
    if getattr(config, "PROXY", ""):
        options.add_argument(f"--proxy-server={config.PROXY}")
        logger.info("Configured web driver proxy server: %s", config.PROXY)
    
    # Enable persistent Chrome profile mapped to this email to save cookies
    import re, os
    clean_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
    profile_path = os.path.abspath(os.path.join(os.path.dirname(__file__), f"chrome_profiles/profile_{clean_email}"))
    options.add_argument(f"--user-data-dir={profile_path}")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-gpu-rasterization")
    options.add_argument("--disable-gpu-driver-bug-workarounds")
    options.add_argument("--disable-impl-side-painting")
    options.add_argument("--disable-accelerated-2d-canvas")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=390,844")
    options.add_argument(f"--user-agent={profile.user_agent}")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--no-first-run")
    options.add_argument("--no-service-autorun")
    options.add_argument("--password-store=basic")
    options.add_argument("--disable-features=Translate,SafeBrowsing")
    options.add_argument("--js-flags=--max-old-space-size=512")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("mobileEmulation", {
        "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0},
        "userAgent": profile.user_agent,
    })

    import os
    # For Docker / Linux server environments, check common paths for Chromium
    for path in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]:
        if os.path.exists(path):
            options.binary_location = path
            logger.info("Found browser binary at %s", path)
            break

    chromedriver_path = None
    for path in ["/usr/bin/chromedriver"]:
        if os.path.exists(path):
            chromedriver_path = path
            logger.info("Found ChromeDriver binary at %s", path)
            break

    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
    else:
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


def _handle_captcha(driver, chat_id) -> bool:
    # Check if a CAPTCHA is present
    captcha_indicators = [
        "type the text you hear or see", "enter the letters", "введите символы",
        "капча", "captcha", "security check"
    ]
    html_content = (driver.page_source or "").lower()
    is_captcha = any(ind in html_content for ind in captcha_indicators) or driver.find_elements(By.CSS_SELECTOR, "img[src*='Captcha']") or driver.find_elements(By.CSS_SELECTOR, "#captcha")
    
    if not is_captcha:
        return False
        
    captcha_field = None
    for sel in ['input[id*="captcha"]', 'input[name*="captcha"]', 'input[type="text"]']:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                captcha_field = el
                break
        except NoSuchElementException:
            continue
            
    if captcha_field:
        logger.info("CAPTCHA detected. Asking user via Telegram...")
        
        screenshot_path = "debug_login_error.png"
        try:
            driver.save_screenshot(screenshot_path)
        except Exception as se:
            logger.warning("Could not save screenshot: %s", se)
            screenshot_path = None

        # Notify Telegram user
        import requests
        import os
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
        try:
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    requests.post(url, data={
                        "chat_id": chat_id,
                        "caption": "🤖 *CAPTCHA Required*\n\nGoogle is asking you to solve a CAPTCHA.\n\nPlease reply to this message with the *characters shown in the image*:",
                        "parse_mode": "Markdown"
                    }, files={"photo": f}, timeout=15)
                os.remove(screenshot_path)
            else:
                raise Exception()
        except Exception:
            url_msg = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url_msg, json={
                "chat_id": chat_id,
                "text": "🤖 *CAPTCHA Required*\n\nGoogle is asking you to solve a CAPTCHA. Please reply to this message with the characters shown in the browser:",
                "parse_mode": "Markdown"
            }, timeout=15)

        # Register event and wait
        import threading
        event = threading.Event()
        config.PENDING_INPUTS[chat_id] = {"event": event, "value": None}
        
        success = event.wait(timeout=90)
        if not success or not config.PENDING_INPUTS[chat_id]["value"]:
            config.PENDING_INPUTS.pop(chat_id, None)
            raise GoogleAutomationError("Timeout waiting for CAPTCHA response.")
            
        captcha_value = config.PENDING_INPUTS[chat_id]["value"]
        config.PENDING_INPUTS.pop(chat_id, None)
        
        # Type CAPTCHA and submit
        captcha_field.clear()
        captcha_field.send_keys(captcha_value)
        time.sleep(1)
        
        # Click Next
        next_button = None
        for sel_btn in ["button[type='submit']", "button", "input[type='submit']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel_btn)
                if el.is_displayed():
                    next_button = el
                    break
            except NoSuchElementException:
                continue
                
        if next_button:
            try:
                next_button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", next_button)
        else:
            captcha_field.submit()
            
        logger.info("Submitted CAPTCHA response. Waiting...")
        time.sleep(6)
        return True
        
    return False


def _handle_phone_prompt(driver, chat_id) -> bool:
    prompt_indicators = [
        "Check your phone", "Tap yes", "open the google app", "confirm on your phone",
        "нажмите да", "откройте приложение", "подтвердите на телефоне", "check your tablet",
        "check your mobile", "проверьте телефон"
    ]
    html_content = (driver.page_source or "").lower()
    is_prompt = any(ind in html_content for ind in prompt_indicators)
    
    if not is_prompt:
        return False
        
    logger.info("Phone prompt/tap screen detected. Notifying user...")
    
    # Take a screenshot so the user sees the number to tap
    screenshot_path = "debug_login_error.png"
    try:
        driver.save_screenshot(screenshot_path)
    except Exception as se:
        logger.warning("Could not save screenshot: %s", se)
        screenshot_path = None

    import requests
    import os
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        if screenshot_path and os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as f:
                requests.post(url, data={
                    "chat_id": chat_id,
                    "caption": "📱 *Google Phone Verification Prompt*\n\nGoogle has sent a notification to your device.\n\nPlease check your phone/tablet and **approve the sign-in** (if Google shows a number, match it with the number in the image above).\n\nReply with `done` or just wait once you approve it on your phone.",
                    "parse_mode": "Markdown"
                }, files={"photo": f}, timeout=15)
            os.remove(screenshot_path)
        else:
            raise Exception()
    except Exception:
        url_msg = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url_msg, json={
            "chat_id": chat_id,
            "text": "📱 *Google Phone Verification Prompt*\n\nGoogle has sent a notification to your device.\n\nPlease check your phone/tablet and **approve the sign-in**.\n\nReply with `done` once approved.",
            "parse_mode": "Markdown"
        }, timeout=15)

    # Register input to let the user reply "done" manually, but we also poll the page in parallel!
    import threading
    event = threading.Event()
    config.PENDING_INPUTS[chat_id] = {"event": event, "value": None}
    
    # We will poll the page status and also wait for the event
    # Let's poll for up to 90 seconds
    for _ in range(30):
        # Wait 3 seconds
        if event.wait(timeout=3.0):
            # User replied manually (e.g. typed "done")
            break
            
        # Check if the page has changed (e.g. redirecting or prompt disappeared)
        try:
            curr_html = (driver.page_source or "").lower()
            is_still_prompt = any(ind in curr_html for ind in prompt_indicators)
            if not is_still_prompt:
                logger.info("Phone prompt approved (page content changed). Resuming...")
                break
        except Exception:
            break
            
    config.PENDING_INPUTS.pop(chat_id, None)
    time.sleep(3)
    return True


def _handle_recovery_email(driver, chat_id) -> bool:
    # 1. Check if we need to click "Confirm your recovery email"
    selection_texts = [
        "Confirm your recovery email",
        "Confirm recovery email",
        "Подтвердите резервный адрес электронной почты",
        "Подтвердите резервную почту"
    ]
    clicked_selection = False
    for text in selection_texts:
        try:
            el = driver.find_element(By.XPATH, f"//*[contains(text(), '{text}')]")
            if el.is_displayed():
                logger.info("Found selection option: '%s'. Clicking it.", text)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(5)
                clicked_selection = True
                break
        except NoSuchElementException:
            continue

    # 2. Check if the recovery email input field is present
    email_selectors = [
        'input[type="email"]',
        'input[id*="email"]',
        'input[name*="email"]',
        'input[name*="recoveryEmail"]',
        'input[autocomplete*="email"]'
    ]
    
    email_field = None
    html_content = (driver.page_source or "").lower()
    is_verification = any(ind in html_content for ind in ["confirm your recovery email", "enter your recovery email", "резервный", "подтвердите"])
    
    if is_verification:
        for sel in email_selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    email_field = el
                    break
            except NoSuchElementException:
                continue

    if email_field:
        logger.info("Recovery email input field found. Asking user via Telegram...")
        
        screenshot_path = "debug_login_error.png"
        try:
            driver.save_screenshot(screenshot_path)
        except Exception as se:
            logger.warning("Could not save screenshot: %s", se)
            screenshot_path = None

        # Notify Telegram user
        import requests
        import os
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
        try:
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    requests.post(url, data={
                        "chat_id": chat_id,
                        "caption": "📧 *Google Verification Required*\n\nGoogle is asking to enter/confirm your recovery email address.\n\nPlease reply to this message with your *full recovery email address*:",
                        "parse_mode": "Markdown"
                    }, files={"photo": f}, timeout=15)
                os.remove(screenshot_path)
            else:
                raise Exception()
        except Exception:
            url_msg = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url_msg, json={
                "chat_id": chat_id,
                "text": "📧 *Google Verification Required*\n\nGoogle is asking to enter/confirm your recovery email address.\n\nPlease reply to this message with your *full recovery email address*:",
                "parse_mode": "Markdown"
            }, timeout=15)

        # Wait for user input
        import threading
        event = threading.Event()
        config.PENDING_INPUTS[chat_id] = {"event": event, "value": None}
        
        success = event.wait(timeout=90)
        if not success or not config.PENDING_INPUTS[chat_id]["value"]:
            config.PENDING_INPUTS.pop(chat_id, None)
            raise GoogleAutomationError("Timeout waiting for recovery email.")
            
        recovery_email = config.PENDING_INPUTS[chat_id]["value"]
        config.PENDING_INPUTS.pop(chat_id, None)
        
        # Type recovery email and click Next
        email_field.clear()
        email_field.send_keys(recovery_email)
        time.sleep(1)
        
        # Click Next
        next_button = None
        for sel_btn in ["button[type='submit']", "button", "input[type='submit']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel_btn)
                if el.is_displayed():
                    next_button = el
                    break
            except NoSuchElementException:
                continue
                
        if next_button:
            try:
                next_button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", next_button)
        else:
            email_field.submit()
            
        logger.info("Submitted recovery email. Waiting for Google...")
        time.sleep(6)
        return True

    return clicked_selection


def _handle_use_another_device(driver, chat_id) -> bool:
    # 1. Check if we are on the selection page containing "Use another phone or computer"
    device_selection_texts = [
        "Use another phone or computer to finish signing in",
        "Use another phone or computer",
        "Использовать другой телефон или компьютер"
    ]
    
    for text in device_selection_texts:
        try:
            el = driver.find_element(By.XPATH, f"//*[contains(text(), '{text}')]")
            if el.is_displayed():
                logger.info("Found selection option: '%s'. Clicking it.", text)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(5)
                return True
        except NoSuchElementException:
            continue
            
    return False


VALID_DIAL_CODES = {
    # 1 digit
    "1", "7",
    # 2 digits
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48", "49",
    "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84",
    "86", "90", "91", "92", "93", "94", "95", "98",
    # 3 digits
    "211", "212", "213", "216", "218", "220", "221", "222", "223", "224", "225", "226", "227", "228", "229",
    "230", "231", "232", "233", "234", "235", "236", "237", "238", "239", "240", "241", "242", "243", "244",
    "245", "246", "247", "248", "249", "250", "251", "252", "253", "254", "255", "256", "257", "258", "260",
    "261", "262", "263", "264", "265", "266", "267", "268", "269", "290", "291", "297", "298", "299",
    "350", "351", "352", "353", "354", "355", "356", "357", "358", "359", "370", "371", "372", "373", "374",
    "375", "376", "377", "378", "380", "381", "382", "383", "385", "386", "387", "389", "420", "421", "423",
    "500", "501", "502", "503", "504", "505", "506", "507", "508", "509", "590", "591", "592", "593", "594",
    "595", "596", "597", "598", "599", "670", "672", "673", "674", "675", "676", "677", "678", "679", "680",
    "681", "682", "683", "685", "686", "687", "688", "689", "690", "691", "692", "850", "852", "853", "855",
    "856", "880", "886", "960", "961", "962", "963", "964", "965", "966", "967", "968", "970", "971", "972",
    "973", "974", "975", "976", "977", "992", "993", "994", "995", "996", "998",
    # 4 digits
    "1242", "1246", "1264", "1268", "1284", "1340", "1345", "1441", "1473", "1649", "1664", "1721", "1758",
    "1767", "1784", "1809", "1829", "1849", "1868", "1869", "1876", "1905"
}


def _handle_recovery_phone(driver, chat_id) -> bool:
    # 1. Check if we are on the selection page
    selection_texts = [
        "Confirm your recovery phone number",
        "Confirm recovery phone",
        "Подтвердите резервный номер телефона",
        "Подтвердите номер телефона",
        "Confirm your recovery phone"
    ]
    
    clicked_selection = False
    for text in selection_texts:
        try:
            el = driver.find_element(By.XPATH, f"//*[contains(text(), '{text}')]")
            if el.is_displayed():
                logger.info("Found selection option: '%s'. Clicking it.", text)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(5)
                clicked_selection = True
                break
        except NoSuchElementException:
            continue

    # 2. Check if the recovery phone input field is present
    phone_selectors = [
        'input[type="tel"]',
        'input[id*="phone"]',
        'input[name*="phone"]',
        'input[name*="phoneNumber"]',
        'input[autocomplete*="phone"]'
    ]
    
    phone_field = None
    for sel in phone_selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                # Make sure it's not the TOTP input field or code field
                if "code" not in (el.get_attribute("id") or "").lower() and "totp" not in (el.get_attribute("id") or "").lower():
                    phone_field = el
                    break
        except NoSuchElementException:
            continue

    if phone_field:
        logger.info("Recovery phone input field found. Asking user via Telegram...")
        
        # Take a screenshot so the user sees Google's screen
        screenshot_path = "debug_login_error.png"
        try:
            driver.save_screenshot(screenshot_path)
            logger.info("Saved recovery phone screen screenshot")
        except Exception as se:
            logger.warning("Could not save screenshot: %s", se)
            screenshot_path = None

        # Notify Telegram user
        import requests
        import os
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
        try:
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    requests.post(url, data={
                        "chat_id": chat_id,
                        "caption": "🔒 *Google Verification Required*\n\nGoogle is asking to confirm your recovery phone number.\n\nPlease reply to this message with your *full recovery phone number* (including country code, e.g. `+1234567890`):",
                        "parse_mode": "Markdown"
                    }, files={"photo": f}, timeout=15)
                os.remove(screenshot_path)
            else:
                raise Exception("No screenshot available")
        except Exception as e:
            logger.warning("Failed to send photo to Telegram: %s. Sending text instead.", e)
            url_msg = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url_msg, json={
                "chat_id": chat_id,
                "text": "🔒 *Google Verification Required*\n\nGoogle is asking to confirm your recovery phone number.\n\nPlease reply to this message with your *full recovery phone number* (including country code, e.g. `+1234567890`):",
                "parse_mode": "Markdown"
            }, timeout=15)

        # Register event and wait
        import threading
        event = threading.Event()
        config.PENDING_INPUTS[chat_id] = {"event": event, "value": None}
        
        # Wait up to 90 seconds for user reply
        success = event.wait(timeout=90)
        if not success or not config.PENDING_INPUTS[chat_id]["value"]:
            config.PENDING_INPUTS.pop(chat_id, None)
            raise GoogleAutomationError("Timeout waiting for recovery phone number.")
            
        phone_number = config.PENDING_INPUTS[chat_id]["value"]
        config.PENDING_INPUTS.pop(chat_id, None)
        
        # Select country code dropdown and type the phone number
        dial_code = None
        local_number = phone_number

        # Clean the phone number (remove spaces and dashes)
        phone_clean = "".join(c for c in phone_number if c.isalnum() or c == "+")
        
        if phone_clean.startswith("+"):
            # Try to match the dial code against valid dial codes
            for l in [4, 3, 2, 1]:
                candidate = phone_clean[1:l+1]
                if candidate in VALID_DIAL_CODES:
                    dial_code = "+" + candidate
                    local_number = phone_clean[l+1:]
                    break
            
            # Fallback if not found in list
            if dial_code is None:
                for l in [4, 3, 2, 1]:
                    candidate = phone_clean[1:l+1]
                    if candidate.isdigit():
                        dial_code = "+" + candidate
                        local_number = phone_clean[l+1:]
                        break
        else:
            # Default to Iran if it looks like an Iranian mobile number
            if phone_clean.startswith("09") and len(phone_clean) == 11:
                dial_code = "+98"
                local_number = phone_clean[1:]
            elif phone_clean.startswith("9") and len(phone_clean) == 10:
                dial_code = "+98"
                local_number = phone_clean

        if dial_code:
            logger.info("Parsed dial code: %s, local number: %s", dial_code, local_number)
            
            # 1. Try to find the dropdown relative to the phone input field (up to 3 levels parent search)
            dropdown = None
            try:
                parent = phone_field.find_element(By.XPATH, "..")
                for depth in range(1, 4):
                    try:
                        el = parent.find_element(By.XPATH, './/div[@role="combobox"] | .//div[@aria-haspopup="listbox"] | .//button | .//div[contains(@aria-label, "code")]')
                        if el.is_displayed():
                            dropdown = el
                            logger.info("Found country dropdown relative to phone field at depth %d", depth)
                            break
                    except NoSuchElementException:
                        parent = parent.find_element(By.XPATH, "..")
            except Exception as e:
                logger.warning("Could not find dropdown relative to phone field: %s", e)

            # Fallback to CSS selectors if parent search failed
            if not dropdown:
                dropdown_selectors = [
                    '[role="combobox"]',
                    '[aria-haspopup="listbox"]',
                    'button[aria-expanded]',
                    '[aria-label*="country"]',
                    '[aria-label*="Country"]',
                    '[aria-label*="code"]',
                    '[aria-label*="Code"]'
                ]
                for sel in dropdown_selectors:
                    try:
                        el = driver.find_element(By.CSS_SELECTOR, sel)
                        if el.is_displayed():
                            dropdown = el
                            break
                    except NoSuchElementException:
                        continue
                    
            if dropdown:
                try:
                    logger.info("Clicking country dropdown...")
                    try:
                        dropdown.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", dropdown)
                    time.sleep(2)
                    
                    # Search for the country option
                    # Build search strings for option (e.g. "+98", "+ 98", "Iran", "Иран")
                    search_terms = [dial_code, dial_code.replace("+", "+ ")]
                    if dial_code == "+98":
                        search_terms.extend(["Iran", "Иран", "Исламская Республика Иран"])
                    
                    # Wait up to 5 seconds for the option to become visible
                    option = None
                    for _ in range(10):
                        for term in search_terms:
                            opt_selectors = [
                                f"//span[contains(text(), '{term}')]",
                                f"//*[@role='option'][contains(., '{term}')]",
                                f"//li[contains(., '{term}')]",
                                f"//div[@role='option']//*[contains(text(), '{term}')]",
                                f"//div[contains(@data-value, '{term}')]"
                            ]
                            for opt_sel in opt_selectors:
                                try:
                                    el_opt = driver.find_element(By.XPATH, opt_sel)
                                    if el_opt.is_displayed():
                                        option = el_opt
                                        break
                                except NoSuchElementException:
                                    continue
                            if option:
                                break
                        if option:
                            break
                        time.sleep(0.5)
                            
                    if option:
                        logger.info("Found option for %s. Clicking it.", dial_code)
                        try:
                            option.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", option)
                        time.sleep(2)
                    else:
                        logger.warning("Country option for %s not found in dropdown.", dial_code)
                        # Close dropdown by clicking the phone field
                        try:
                            phone_field.click()
                        except Exception:
                            pass
                        time.sleep(1)
                except Exception as e:
                    logger.warning("Error during country selection: %s", e)

        # Clear and type the phone number
        from selenium.webdriver.common.keys import Keys
        try:
            phone_field.click()
            time.sleep(0.5)
            phone_field.send_keys(Keys.CONTROL + "a")
            phone_field.send_keys(Keys.BACKSPACE)
            time.sleep(0.5)
        except Exception:
            try:
                phone_field.clear()
            except Exception:
                pass
                
        phone_field.send_keys(local_number)
        time.sleep(1)
        
        # Find and click Next/Send button
        next_button = None
        
        # 1. Prioritize buttons with submit action or text labels matching Next/Send
        for term in ["Send", "Next", "Далее", "Отправить", "Отправить код"]:
            try:
                el = driver.find_element(By.XPATH, f"//button[contains(., '{term}')] | //input[contains(@value, '{term}')]")
                if el.is_displayed():
                    next_button = el
                    break
            except NoSuchElementException:
                continue
                
        # 2. Fallback to generic selectors
        if not next_button:
            for sel_btn in ["#phoneNumberNext", "button[type='submit']", "button[jsname]", "#idvPreregisteredPhoneNext", "button"]:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel_btn)
                    if el.is_displayed():
                        next_button = el
                        break
                except NoSuchElementException:
                    continue
                
        if next_button:
            try:
                next_button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", next_button)
        else:
            phone_field.submit()
            
        logger.info("Submitted recovery phone number. Waiting for Google...")
        time.sleep(6)
        return True

    return clicked_selection


def _handle_sms_code(driver, chat_id) -> bool:
    # Check if page is asking for an SMS/phone verification code
    sms_indicators = [
        "Enter the code", "Enter verification code", "A text message with a verification code",
        "Введите код", "Введите код подтверждения", "Код подтверждения отправлен",
        "Enter the 6-digit code", "Enter 6-digit"
    ]
    html_content = (driver.page_source or "")
    is_sms_page = any(ind.lower() in html_content.lower() for ind in sms_indicators)
    
    if not is_sms_page:
        return False
        
    sms_selectors = [
        'input[id*="code"]',
        'input[id*="pin"]',
        'input[autocomplete="one-time-code"]',
        'input[type="tel"]'
    ]
    
    sms_field = None
    for sel in sms_selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                sms_field = el
                break
        except NoSuchElementException:
            continue
            
    if sms_field:
        logger.info("SMS/Verification code input field found. Asking user via Telegram...")
        
        # Take a screenshot
        screenshot_path = "debug_login_error.png"
        try:
            driver.save_screenshot(screenshot_path)
            logger.info("Saved SMS verification screen screenshot")
        except Exception as se:
            logger.warning("Could not save screenshot: %s", se)
            screenshot_path = None

        # Notify Telegram user
        import requests
        import os
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
        try:
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    requests.post(url, data={
                        "chat_id": chat_id,
                        "caption": "💬 *Google Verification Code Sent*\n\nGoogle has sent a verification code to your phone.\n\nPlease reply to this message with the *verification code* (e.g. `G-123456` or just the numbers):",
                        "parse_mode": "Markdown"
                    }, files={"photo": f}, timeout=15)
                os.remove(screenshot_path)
            else:
                raise Exception("No screenshot available")
        except Exception as e:
            logger.warning("Failed to send photo to Telegram: %s. Sending text instead.", e)
            url_msg = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url_msg, json={
                "chat_id": chat_id,
                "text": "💬 *Google Verification Code Sent*\n\nGoogle has sent a verification code to your phone.\n\nPlease reply to this message with the *verification code* (e.g. `G-123456` or just the numbers):",
                "parse_mode": "Markdown"
            }, timeout=15)

        # Register event and wait
        import threading
        event = threading.Event()
        config.PENDING_INPUTS[chat_id] = {"event": event, "value": None}
        
        # Wait up to 90 seconds for user reply
        success = event.wait(timeout=90)
        if not success or not config.PENDING_INPUTS[chat_id]["value"]:
            config.PENDING_INPUTS.pop(chat_id, None)
            raise GoogleAutomationError("Timeout waiting for SMS verification code.")
            
        code = config.PENDING_INPUTS[chat_id]["value"]
        config.PENDING_INPUTS.pop(chat_id, None)
        
        # Clean up code format if user included G-
        if code.lower().startswith("g-"):
            code = code[2:]
        
        # Type the code and submit
        sms_field.clear()
        sms_field.send_keys(code)
        time.sleep(1)
        
        # Find and click Next button
        next_button = None
        for sel_btn in ["#idvPreregisteredPhoneNext", "button[type='submit']", "button", "input[type='submit']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel_btn)
                if el.is_displayed():
                    next_button = el
                    break
            except NoSuchElementException:
                continue
                
        if next_button:
            try:
                next_button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", next_button)
        else:
            sms_field.submit()
            
        logger.info("Submitted SMS code. Waiting for Google...")
        time.sleep(6)
        return True
        
    return False


def _do_login(driver, email, password, totp_key="", chat_id=0):
    from urllib.parse import urlparse
    driver.get(config.GMAIL_LOGIN_URL)
    time.sleep(4)

    # --- Pre-authenticated Session Bypass ---
    # Check if Chrome is already logged into the Google Account (Google dashboard, mail, or one page)
    # If the URL is not a login/signin page, it means our saved session cookies are active!
    hostname = urlparse(driver.current_url).hostname or ""
    if any(h in hostname for h in ["myaccount.google.com", "one.google.com", "mail.google.com"]) and not "/signin" in driver.current_url:
        logger.info("Already logged in via saved cookies! Skipping login credentials entry.")
        return True

    # Standard login typing steps
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

    # Handle recovery phone, email, phone prompt, captcha, or SMS code challenges if they appear
    if chat_id:
        for _ in range(5):
            if _handle_captcha(driver, chat_id):
                continue
            if _handle_use_another_device(driver, chat_id):
                continue
            if _handle_phone_prompt(driver, chat_id):
                continue
            if _handle_recovery_phone(driver, chat_id):
                continue
            if _handle_recovery_email(driver, chat_id):
                continue
            if _handle_sms_code(driver, chat_id):
                continue
            break

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
            if "LOCKED:" in href or "/benefits/" in href:
                continue
            if any(kw in text for kw in keywords) and href:
                return href
        except Exception:
            continue
    pat = re.compile(r"(gemini|upgrade|activate|offer|redeem|trial|checkout)", re.IGNORECASE)
    for link in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = link.get_attribute("href") or ""
            if "LOCKED:" in href or "/benefits/" in href:
                continue
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
    "store.google.com",
]


def _find_checkout_url_after_clicks(driver) -> Optional[str]:
    # 1. Check if the current URL is already a checkout page
    cur_url = driver.current_url
    if any(pat in cur_url for pat in OFFER_URL_PATTERNS):
        logger.info("Current URL is already a checkout page: %s", cur_url)
        return cur_url

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

            # Quick wait (1.5 seconds) to see if anything starts loading/redirecting/modal
            time.sleep(1.5)

            # 1. Check if any new tab/window was opened
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
                                driver.close()
                                driver.switch_to.window(original_handle)
                                return new_url
                            driver.close()
                        except Exception as w_err:
                            logger.warning("Tab check error: %s", w_err)
                driver.switch_to.window(original_handle)

            # 2. Check if main window URL changed into a checkout page
            cur_url = driver.current_url
            if any(pat in cur_url for pat in OFFER_URL_PATTERNS):
                logger.info("Captured checkout link from redirection: %s", cur_url)
                return cur_url

            # 3. Check if any UCP checkout iframe widget opened on screen
            for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    src = iframe.get_attribute("src") or ""
                    if any(pat in src for pat in OFFER_URL_PATTERNS):
                        logger.info("Captured checkout link from iframe src: %s", src)
                        return src
                    
                    # Inspect internal frame anchor links
                    driver.switch_to.frame(iframe)
                    for link_el in driver.find_elements(By.TAG_NAME, "a"):
                        try:
                            href = link_el.get_attribute("href") or ""
                            if any(pat in href for pat in OFFER_URL_PATTERNS):
                                logger.info("Captured checkout link inside iframe DOM: %s", href)
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
                    continue
        except Exception as e:
            logger.warning("Candidate click execution error: %s", e)
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            continue

    return None


def _check_google_one(driver):
    # Search one.google.com/offers first, as that contains active claim buttons when logged in!
    for url in ("https://one.google.com/offers", config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(5)
            
            # Skip 404 pages immediately to avoid clicking links on error pages
            html_content = (driver.page_source or "").lower()
            if "ошибка 404" in html_content or "error 404" in html_content or "404" in driver.title:
                logger.warning("404 Page detected at %s. Skipping url...", url)
                continue

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
                       totp_key: str = "", chat_id: int = 0) -> Optional[str]:
    """
    Login + find Gemini Pro offer. Runs with 90s timeout.
    """
    driver = None
    try:
        driver = _build_driver(device, email)

        if not _do_login(driver, email, password, totp_key, chat_id):
            try:
                driver.save_screenshot("debug_login_error.png")
                logger.info("Saved debug screenshot to debug_login_error.png")
            except Exception as se:
                logger.warning("Could not save screenshot: %s", se)
            raise GoogleAutomationError("Login failed - check credentials")

        logger.info("Logged in, searching Google One...")
        link = _check_google_one(driver)
        return link

    except Exception as exc:
        if driver and not os.path.exists("debug_login_error.png"):
            try:
                driver.save_screenshot("debug_login_error.png")
                logger.info("Saved debug screenshot on exception to debug_login_error.png")
            except Exception as se:
                logger.warning("Could not save screenshot: %s", se)
        
        if isinstance(exc, GoogleAutomationError):
            raise exc
            
        exc_str = str(exc)
        if any(msg in exc_str or msg in type(exc).__name__ for msg in ["Remote end closed connection", "Connection aborted", "ProtocolError", "MaxRetryError", "chrome not reachable", "disconnected"]):
            raise GoogleAutomationError(
                "The browser crashed or the connection was lost. This is usually caused by low system memory (RAM)."
            ) from exc
            
        raise GoogleAutomationError(f"Automation error: {exc}") from exc
    finally:
        if chat_id:
            config.PENDING_INPUTS.pop(chat_id, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
