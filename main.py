import argparse
import json
import base64
import pandas as pd
import os
import pickle
import time
import re
import smtplib
import sys
import traceback
from collections import Counter
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from webdriver_manager.chrome import ChromeDriverManager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

def check_exists_by_xpath(xpath: str, webdriver: webdriver.Chrome) -> bool:
    """Check if an element exists by XPATH. Currently unused, but may be useful in the future and cutdown gather_data codeblock."""
    try:
        webdriver.find_element_by_xpath(xpath)
    except NoSuchElementException:
        return False
    return True


def extract_labeled_amount(text: str, label_pattern: str) -> float | None:
    """Read one currency amount close to a known label, returning None if unclear."""
    amount_pattern = r'(?:USD\s*|\$\s*)?((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?:\s*USD)?'
    amounts = set()
    for label in re.finditer(label_pattern, text, re.IGNORECASE):
        # The amount is commonly on the same rendered-text line as the label.
        # Check only a short window after the label so unrelated totals do not match.
        nearby = text[label.end():label.end() + 100]
        match = re.search(amount_pattern, nearby, re.IGNORECASE)
        if match:
            amounts.add(float(match.group(1).replace(',', '')))
    return amounts.pop() if len(amounts) == 1 else None


def get_payment_summary(
    status_text: str,
    panel_text: str,
    payment_details_text: str = '',
    reservation_cancelled: bool = False,
) -> dict:
    """Calculate card balance from Expedia's labeled payout and charged amounts."""
    charge_window_passed = bool(re.search(
        r"\b180-day\s+charge\s+window\s+for\s+this\s+card\s+has\s+passed\b",
        panel_text, re.IGNORECASE,
    ))
    payment = {
        'Amount Due': None,
        'Charge Window Passed': charge_window_passed,
        'Reservation Canceled': reservation_cancelled,
        'Needs Review': False,
        'Review Note': '',
    }
    if status_text == 'Charged in full':
        payment['Amount Due'] = 0.0
        return payment

    notes = []
    if reservation_cancelled:
        notes.append('Reservation is canceled; verify card charge eligibility.')
    if charge_window_passed:
        notes.append('The 180-day charge window has passed.')
    if status_text in {'Refund due', 'Deactivated'}:
        payment['Amount Due'] = 0.0
        payment['Needs Review'] = bool(notes)
        payment['Review Note'] = ' '.join(notes)
        return payment
    if status_text == 'Virtual card unavailable':
        notes.append('Expedia could not display the virtual card information; verify its status and amount manually.')
        payment['Needs Review'] = True
        payment['Review Note'] = ' '.join(notes)
        return payment
    if status_text not in {'Ready to charge', 'Partially charged'}:
        notes.append(f'Unrecognized card status: {status_text!r}.')
    else:
        payout = extract_labeled_amount(
            payment_details_text, r'Your total payout\b',
        )
        if status_text == 'Ready to charge':
            if payout is not None:
                payment['Amount Due'] = payout
            else:
                notes.append('Your total payout could not be read from payment details.')
        else:
            charged = extract_labeled_amount(
                panel_text, r"You(?:'|’)ve charged\b",
            )
            if payout is None:
                notes.append('Your total payout could not be read from payment details.')
            if charged is None:
                notes.append('The amount already charged could not be read from the card panel.')
            if payout is not None and charged is not None:
                remaining = round(payout - charged, 2)
                if remaining < 0:
                    notes.append('The amount already charged exceeds the total payout.')
                else:
                    payment['Amount Due'] = remaining

    payment['Needs Review'] = bool(notes)
    payment['Review Note'] = ' '.join(notes)
    return payment


def read_payment_details_in_new_tab(driver: webdriver.Chrome, row_context: str) -> str:
    """Open the reservation's payment details tab and return its rendered text."""
    original_handle = driver.current_window_handle
    old_handles = set(driver.window_handles)
    payment_link = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
        By.XPATH,
        "//*[@id='sidePanelDialog']//*[self::a or self::button]"
        "[normalize-space(.)='See payment details']",
    )))
    payment_link.click()
    WebDriverWait(driver, 20).until(lambda browser: len(set(browser.window_handles) - old_handles) > 0)
    new_handle = next(iter(set(driver.window_handles) - old_handles))
    driver.switch_to.window(new_handle)

    def find_payment_text(browser):
        # Expedia may render the legacy reservation details inside an iframe.
        # Search visible body text in the top document and its frames.
        try:
            browser.switch_to.default_content()
            body_text = browser.find_element(By.TAG_NAME, 'body').text
            if 'Your total payout' in body_text:
                return body_text
            frames = browser.find_elements(By.TAG_NAME, 'iframe')
            for frame in frames:
                try:
                    browser.switch_to.default_content()
                    browser.switch_to.frame(frame)
                    body_text = browser.find_element(By.TAG_NAME, 'body').text
                    if 'Your total payout' in body_text:
                        return body_text
                    nested_frames = browser.find_elements(By.TAG_NAME, 'iframe')
                    for nested_frame in nested_frames:
                        browser.switch_to.default_content()
                        browser.switch_to.frame(frame)
                        browser.switch_to.frame(nested_frame)
                        body_text = browser.find_element(By.TAG_NAME, 'body').text
                        if 'Your total payout' in body_text:
                            return body_text
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
            return False
        finally:
            browser.switch_to.default_content()

    page_text = WebDriverWait(driver, 60).until(
        find_payment_text,
        message=f"Timed out while loading payment details ({row_context}); payout text was not visible in the page or its frames.",
    )
    driver.close()
    driver.switch_to.window(original_handle)
    return page_text


def should_include_reservation(status_text: str, payment: dict) -> bool:
    """Keep unresolved balances visible without treating them as known amounts."""
    if status_text == 'Charged in full':
        return False
    if payment.get('Reservation Canceled'):
        return True
    if status_text in {'Refund due', 'Deactivated'}:
        return False
    if payment['Needs Review'] or status_text == 'Ready to charge':
        return True
    amount_due = payment['Amount Due']
    return status_text == 'Partially charged' and amount_due is not None and amount_due >= 5


def submit_password_with_retry(driver: webdriver.Chrome, password: str, max_retries: int = 2) -> None:
    """Submit the Expedia password and retry only when its transient error banner appears."""
    password_input = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located((By.NAME, 'password'))
    )
    password_input.send_keys(password)
    continue_button = (By.XPATH, "//button[normalize-space(.)='Continue']")

    def error_banner_visible(browser) -> bool:
        try:
            return 'something went wrong' in browser.find_element(By.TAG_NAME, 'body').text.casefold()
        except NoSuchElementException:
            return False

    def password_page_visible(browser) -> bool:
        try:
            return any(element.is_displayed() for element in browser.find_elements(By.NAME, 'password'))
        except StaleElementReferenceException:
            return True

    for attempt in range(max_retries + 1):
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable(continue_button)).click()
        try:
            WebDriverWait(driver, 8).until(
                lambda browser: error_banner_visible(browser) or not password_page_visible(browser)
            )
        except TimeoutException:
            # Leave an unchanged page for the existing visible login confirmation step.
            return

        if not error_banner_visible(driver):
            return
        if attempt < max_retries:
            print(f"Expedia showed a temporary sign-in error; retrying Continue ({attempt + 1}/{max_retries}).")
        else:
            print("Expedia still shows a sign-in error after retries; inspect Chrome before continuing.")


def submit_email_with_retry(driver: webdriver.Chrome, user_name: str, max_retries: int = 2) -> None:
    """Submit the Expedia email and retry only when its transient error banner appears."""
    email_input = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located((By.NAME, 'username'))
    )
    email_input.send_keys(user_name)
    continue_button = (By.XPATH, "//button[normalize-space(.)='Continue']")

    def error_banner_visible(browser) -> bool:
        try:
            return 'something went wrong' in browser.find_element(By.TAG_NAME, 'body').text.casefold()
        except NoSuchElementException:
            return False

    def email_page_visible(browser) -> bool:
        try:
            return any(element.is_displayed() for element in browser.find_elements(By.NAME, 'username'))
        except StaleElementReferenceException:
            return True

    for attempt in range(max_retries + 1):
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable(continue_button)).click()
        try:
            WebDriverWait(driver, 8).until(
                lambda browser: error_banner_visible(browser) or not email_page_visible(browser)
            )
        except TimeoutException:
            return

        if not error_banner_visible(driver):
            return
        if attempt < max_retries:
            print(f"Expedia showed a temporary sign-in error; retrying Continue ({attempt + 1}/{max_retries}).")
        else:
            print("Expedia still shows a sign-in error after retries; inspect Chrome before continuing.")


def save_auth_cookies(driver: webdriver.Chrome) -> None:
    """Refresh the local cookie backup after the user confirms a successful sign-in."""
    local_data_dir = 'local-data'
    os.makedirs(local_data_dir, exist_ok=True)
    with open(os.path.join(local_data_dir, 'cookies.pkl'), 'wb') as cookie_file:
        pickle.dump(driver.get_cookies(), cookie_file)

def generate_monthly_date_ranges(start_date: str, end_date: str) -> list:
    """Generate monthly date ranges between two dates."""
    current = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    
    date_ranges = []
    while current < end:
        month_end = (current.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        if month_end > end:
            month_end = end
        date_ranges.append((current.strftime('%Y-%m-%d'), month_end.strftime('%Y-%m-%d')))
        current = month_end + timedelta(days=1)
    
    return date_ranges

def build_payload(date_from: str, date_to: str) -> str:
    """Build and encode JSON payload."""
    data = {
        "searchCriteria": {
            "booked": True,
            "bookingItemId": None,
            "canceled": True,
            "confirmationNumber": None,
            "confirmed": True,
            "dateFrom": date_from,
            "dateTo": date_to,
            "dateType": "checkIn",
            "expediaCollect": True,
            "firstName": None,
            "hotelCollect": False,
            "reconciled": False,
            "readyToReconcile": False,
            "isSpecialRequest": False,
            "isVIPBooking": False,
            "evc": True,
            "langId": 0,
            "lastName": None,
            "pageNumber": None,
            "resultsPerPage": None,
            "returnBookingItemIDsOnly": False,
            "sortDirection": None,
            "sortField": None,
            "timezoneOffset": "-07:00",
            "unconfirmed": True,
            "wantLocal": True,
            "searchTerm": None,
            "bookingItemIds": [],
            "searchParam": None,
            "filtersApplied": True,
            "appliedFilters": ["expediaCollect", "evc"],
            "filterByDateBookedOn": True,
            "filterByDateCancelledOn": False,
            "filterByDateCheckedIn": True,
            "filterByDateCheckedOut": True,
            "onlySearchCancelWaivers": False
        }
    }
    json_string = json.dumps(data)
    base64_encoded = base64.b64encode(json_string.encode('utf-8')).decode('utf-8')
    return base64_encoded

def wait_for_reservation_element(driver, locator, stage: str, timeout: int = 20):
    """Wait for a visible element, stopping if Expedia reports a loading failure."""
    error_xpath = (
        "//*[contains(normalize-space(.), 'reservations cannot be loaded') "
        "and not(.//*[contains(normalize-space(.), 'reservations cannot be loaded')])]"
    )

    def ready(browser):
        try:
            for error in browser.find_elements(By.XPATH, error_xpath):
                if error.is_displayed():
                    raise RuntimeError(
                        f"Expedia reported that reservations cannot be loaded while {stage}. "
                        "The scan stopped without completing this date range."
                    )
            return EC.visibility_of_element_located(locator)(browser)
        except StaleElementReferenceException:
            # Expedia can replace elements while its page is rendering.
            return False

    try:
        return WebDriverWait(driver, timeout).until(ready)
    except TimeoutException as exc:
        raise TimeoutException(
            f"Timed out while {stage}. The expected element was not visible; "
            "this date range was not completed."
        ) from exc


RESERVATION_TABLE_XPATH = '//*[@id="content"]/div/div[3]/div/div/div[2]/div/div/div[2]/table/tbody'
NEXT_PAGE_XPATH = '//*[@id="content"]/div/div[3]/div/div/div[2]/div/nav/div/div[2]/button'


def get_reservation_rows(driver, stage: str):
    """Wait for and return the currently visible reservation rows."""
    wait_for_reservation_element(
        driver, (By.XPATH, RESERVATION_TABLE_XPATH), stage, timeout=45,
    )
    return WebDriverWait(driver, 25).until(
        lambda browser: browser.find_elements(
            By.XPATH, f'{RESERVATION_TABLE_XPATH}/tr'
        ) or False
    )


def restore_reservation_page(
    driver, search_url: str, page_number: int, target_row_text: str,
    row_context: str,
):
    """Reload the search, restore its UI page, and locate the failed row by its text."""
    driver.get(search_url)
    rows = get_reservation_rows(driver, f"restoring reservations ({row_context})")

    for page_to_open in range(2, page_number + 1):
        next_button = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.XPATH, NEXT_PAGE_XPATH))
        )
        if "disabled" in (next_button.get_attribute("class") or "").casefold() or next_button.get_attribute("disabled") is not None:
            raise RuntimeError(
                f"Could not restore page {page_number} while recovering from {row_context}; "
                f"the Next button stopped at page {page_to_open - 1}."
            )
        old_first_text = rows[0].text
        next_button.click()
        WebDriverWait(driver, 25).until(
            lambda browser: (
                (visible_rows := browser.find_elements(
                    By.XPATH, f'{RESERVATION_TABLE_XPATH}/tr'
                )) and visible_rows[0].text != old_first_text
            )
        )
        rows = get_reservation_rows(
            driver, f"restoring page {page_to_open} ({row_context})",
        )

    if not target_row_text:
        return rows, 1

    normalized_target = " ".join(target_row_text.split())
    matches = [
        index for index, row in enumerate(rows, start=1)
        if " ".join(row.text.split()) == normalized_target
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Recovered the reservations page for {row_context}, but could not uniquely "
            "identify the failed row from its visible reservation text. The scan stopped "
            "with its completed results saved."
        )
    return rows, matches[0]


def read_reservation_status(driver, row_context: str) -> tuple[str, str]:
    """Read the status text from the rendered reservation panel."""
    known_statuses = (
        'Charged in full', 'Partially charged', 'Ready to charge',
        'Refund due', 'Deactivated',
    )

    def find_status(browser):
        try:
            panel_text = browser.find_element(By.ID, 'sidePanelDialog').text
        except (NoSuchElementException, StaleElementReferenceException):
            return False
        lines = [line.strip() for line in panel_text.splitlines() if line.strip()]
        for status in known_statuses:
            if any(line.casefold() == status.casefold() for line in lines):
                return status, panel_text
        if 'able to display your virtual card information' in panel_text.casefold():
            return 'Virtual card unavailable', panel_text
        for status in ('Canceled', 'Cancelled'):
            if any(line.casefold() == status.casefold() for line in lines):
                return status, panel_text
        return False

    try:
        return WebDriverWait(driver, 20).until(find_status)
    except TimeoutException:
        # Preserve the panel for review rather than abandoning the entire scan
        # when Expedia introduces a new status label.
        try:
            panel_text = driver.find_element(By.ID, 'sidePanelDialog').text
        except NoSuchElementException as exc:
            raise RuntimeError(
                f"Reservation details panel disappeared while reading the card status "
                f"({row_context})."
            ) from exc
        if 'able to display your virtual card information' in panel_text.casefold():
            return 'Virtual card unavailable', panel_text
        return 'Unrecognized status', panel_text


def refresh_virtual_card_if_available(
    driver, row_context: str, debug: bool = False, before_refresh=None,
) -> None:
    """Use Expedia's one-shot card reload control, then wait for its status to render."""
    lower = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    lower_label = "translate(concat(@aria-label, ' ', @title), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    reload_xpath = (
        "//*[@id='sidePanelDialog']//*[self::button or self::a or @role='button']["
        f"contains({lower}, 'reload') or contains({lower}, 'refresh') or "
        f"contains({lower_label}, 'reload') or contains({lower_label}, 'refresh')]"
    )
    reload_buttons = [
        element for element in driver.find_elements(By.XPATH, reload_xpath)
        if element.is_displayed() and element.is_enabled()
    ]
    if not reload_buttons:
        return

    reload_button = reload_buttons[0]
    if before_refresh:
        before_refresh()
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
        reload_button,
    )
    reload_button.click()
    if debug:
        print(f"Reloading the virtual card box once: {row_context}")

    card_statuses = {'Charged in full', 'Partially charged', 'Ready to charge', 'Refund due', 'Deactivated'}

    def card_status_visible(browser):
        try:
            lines = {
                line.strip().casefold()
                for line in browser.find_element(By.ID, 'sidePanelDialog').text.splitlines()
                if line.strip()
            }
            return any(status.casefold() in lines for status in card_statuses)
        except (NoSuchElementException, StaleElementReferenceException):
            return False

    try:
        WebDriverWait(driver, 45).until(card_status_visible)
    except TimeoutException:
        try:
            driver.find_element(By.ID, 'sidePanelDialog')
        except NoSuchElementException as exc:
            raise RuntimeError(
                f"Reservation details panel disappeared while refreshing the virtual card "
                f"({row_context})."
            ) from exc
        if debug:
            print(f"Card status did not appear after reload; keeping the reservation for review: {row_context}")


def read_optional_panel_text(driver, xpath: str) -> str:
    """Read a panel field when present; return blank if Expedia omitted it."""
    try:
        return driver.find_element(By.XPATH, xpath).text.strip()
    except (NoSuchElementException, StaleElementReferenceException):
        return ''


def capture_reservation_identity(driver, row_text: str, guest_name: str) -> dict:
    """Keep reservation identifiers before a card refresh can remove the panel."""
    date_text = read_optional_panel_text(
        driver, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[2]/div[1]/div/div[1]/div',
    )
    check_in = check_out = None
    try:
        check_in_text, check_out_text = date_text.splitlines()[0].split(' - ')
        check_in = datetime.strptime(check_in_text, '%m/%d/%Y')
        check_out = datetime.strptime(check_out_text, '%m/%d/%Y')
    except (IndexError, ValueError):
        pass

    expedia_number = read_optional_panel_text(
        driver, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[1]/header/h3/span/span',
    ).lstrip('#')
    if not expedia_number:
        match = re.search(r'\b\d{9,12}\b', row_text)
        expedia_number = match.group() if match else ''

    panel_text = read_optional_panel_text(driver, '//*[@id="sidePanelDialog"]')
    return {
        'Check In Date': check_in,
        'Check Out Date': check_out,
        'Guest Name': read_optional_panel_text(
            driver, '//*[@id="sidePanelDialog"]/div/div/div/div/div[1]/div[1]/div[2]/div',
        ) or guest_name,
        'Expedia Number': expedia_number,
        'Hotel Number': read_optional_panel_text(
            driver, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[2]/div[2]/div/div/div[2]/div/span',
        ).lstrip('#'),
        'Reservation Canceled': any(
            line.strip().casefold() in {'canceled', 'cancelled'}
            for line in panel_text.splitlines()
        ),
        'Charge Window Passed': bool(re.search(
            r'\b180-day\s+charge\s+window\s+for\s+this\s+card\s+has\s+passed\b',
            panel_text, re.IGNORECASE,
        )),
    }


def gather_data(
    data: pd.DataFrame, driver: webdriver.Chrome, prop_id: str,
    start: str, end: str, debug: bool = False,
    checkpoint_path: str | None = None,
) -> pd.DataFrame:
    """Gather data from the reservation table."""
    status_counts = Counter()
    initial_report_rows = len(data)
    initial_review_rows = int(data['Needs Review'].fillna(False).sum()) if 'Needs Review' in data else 0
    payload = build_payload(start, end)
    # print(f"Payload: {payload}")

    search_url = f"https://apps.expediapartnercentral.com/lodging/bookings?htid={prop_id}#{payload}"
    driver.get(search_url)
    time.sleep(20)

    # Runs until the 'Next' button is disabled
    page_number = 1
    while True:
        scan_context = f"{start} to {end}, page {page_number}"
        page_load_retries = 0
        while True:
            try:
                rows = get_reservation_rows(
                    driver, f"loading the reservations table ({scan_context})",
                )
                break
            except RuntimeError as exc:
                if "reservations cannot be loaded" not in str(exc).casefold() or page_load_retries >= 2:
                    raise
                page_load_retries += 1
                print(
                    f"Expedia could not load {scan_context}; reloading the search "
                    f"(attempt {page_load_retries} of 2)."
                )
                try:
                    rows, _ = restore_reservation_page(
                        driver, search_url, page_number, "", scan_context,
                    )
                except RuntimeError as recovery_error:
                    if "reservations cannot be loaded" in str(recovery_error).casefold() and page_load_retries < 2:
                        continue
                    raise
        # print(rows)

        # Loop through each row and interact with the button
        row_number = 1
        row_retry_count = 0
        while row_number <= len(rows):
            row = rows[row_number - 1]
            row_context = f"{scan_context}, row {row_number} of {len(rows)}"
            original_row_text = row.text
            review_identity = {}
            card_unavailable_visible = False
            if debug:
                print(f"Opening reservation details: {row_context}")
            # Find the button within the row
            try:
                guest_name_button = row.find_element(By.CLASS_NAME, 'fds-button2.link.guestNameLink')
                row_guest_name = guest_name_button.text.strip()
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                    guest_name_button,
                )
                WebDriverWait(driver, 10).until(EC.element_to_be_clickable(guest_name_button)).click()
                # Preserve the existing request pacing while diagnosing loading failures.
                time.sleep(10)
                wait_for_reservation_element(
                    driver, (By.ID, "sidePanelDialog"),
                    f"opening the reservation details panel ({row_context})",
                )
                card_unavailable_visible = (
                    'able to display your virtual card information'
                    in read_optional_panel_text(driver, '//*[@id="sidePanelDialog"]').casefold()
                )
                if card_unavailable_visible:
                    review_identity.update(capture_reservation_identity(
                        driver, original_row_text, row_guest_name,
                    ))
                if debug:
                    print(f"Details panel opened; waiting for card status: {row_context}")
                refresh_virtual_card_if_available(
                    driver, row_context, debug=debug,
                    before_refresh=lambda: review_identity.update(
                        capture_reservation_identity(driver, original_row_text, row_guest_name)
                    ),
                )
                status_text, panel_text = read_reservation_status(driver, row_context)
                status_counts[status_text] += 1
                reservation_cancelled = any(
                    line.strip().casefold() in {'canceled', 'cancelled'}
                    for line in panel_text.splitlines()
                )
                has_virtual_card = 'virtual card' in panel_text.casefold()

                if status_text in {'Canceled', 'Cancelled', 'Unrecognized status'}:
                    if not has_virtual_card:
                        if debug:
                            print(f"Skipping reservation without a recognized card status or virtual card: {row_context}")
                        driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button').click()
                        row_number += 1
                        row_retry_count = 0
                        if checkpoint_path:
                            data.to_csv(checkpoint_path, index=False)
                        continue

                payment_details_text = ''
                if status_text in {'Ready to charge', 'Partially charged'}:
                    payment_details_text = read_payment_details_in_new_tab(driver, row_context)
                payment = get_payment_summary(
                    status_text, panel_text, payment_details_text,
                    reservation_cancelled=reservation_cancelled,
                )
                amount_due = payment['Amount Due']

                check_in_date_str, check_out_date_str = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[2]/div[1]/div/div[1]/div').text.split('\n')[0].split(' - ')
                
                # Printing all rows for debugging only, remove after done
                check_in_date = datetime.strptime(check_in_date_str, '%m/%d/%Y')
                check_out_date = datetime.strptime(check_out_date_str, '%m/%d/%Y')
                guest_name = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[1]/div[1]/div[2]/div').text.strip()
                expedia_number = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[1]/header/h3/span/span').text.strip().lstrip('#')
                if status_text == 'Virtual card unavailable':
                    cc_number = cc_exp = ''
                else:
                    cc_number = read_optional_panel_text(
                        driver,
                        '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[2]',
                    )
                    cc_number = ''.join(cc_number.split())
                    cc_exp = read_optional_panel_text(
                        driver,
                        '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[4]/div[1]',
                    )
                if status_text != 'Virtual card unavailable' and (not cc_number or not cc_exp):
                    payment['Needs Review'] = True
                    missing_card_detail_note = 'Virtual card details were not fully read.'
                    payment['Review Note'] = ' '.join(
                        note for note in (payment['Review Note'], missing_card_detail_note) if note
                    )
                if debug and payment['Needs Review']:
                    print(f"Review needed ({row_context}): {payment['Review Note']}")

                if should_include_reservation(status_text, payment):
                    try: # Not all reservations might have a hotel confirmation number
                        hotel_confirmation = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[2]/div[2]/div/div/div[2]/div/span').text.strip().lstrip('#')
                    except (NoSuchElementException):
                        hotel_confirmation = None

                    # print(f"Check In Date: {check_in_date}, Check Out Date: {check_out_date}, Guest Name: {guest_name}, Expedia Number: {expedia_number}, Hotel Number: {hotel_confirmation}, Credit Card Number: {cc_number}, Credit Card Expiry: {cc_exp}, Amount Due: {amount_due}")
                    
                    # Create a DataFrame for the new row
                    new_row = pd.DataFrame([{
                        'Check In Date': check_in_date,
                        'Check Out Date': check_out_date,
                        'Guest Name': guest_name,
                        'Expedia Number': expedia_number,
                        'Hotel Number': hotel_confirmation,
                        'Status': status_text,
                        'Credit Card Number': cc_number,
                        'Credit Card Expiry': cc_exp,
                        **payment,
                    }])

                    # Append the new row to the existing DataFrame
                    data = pd.concat([data, new_row], ignore_index=True)
                    if checkpoint_path:
                        data.to_csv(checkpoint_path, index=False)

                # exit_button = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button')
                # exit_button.click()
                # time.sleep(5)


            except (NoSuchElementException, TimeoutException) as e:
                if debug:
                    print(f"Reservation extraction stopped: {row_context}")
                    raise
                print(f"Element not found (main try-except): {e}")
                os.makedirs(os.path.join('local-data', 'screenshots'), exist_ok=True)
                driver.save_screenshot(os.path.join(
                    'local-data', 'screenshots',
                    f'{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}_error_screenshot.png',
                ))
                # exit_button = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button') # Exit the side panel dialog to avoid stuck panels even during element missing errors
                # exit_button.click()
                pass
            except RuntimeError as exc:
                error_text = str(exc).casefold()
                recoverable_failure = (
                    "reservations cannot be loaded" in error_text
                    or "reservation details panel disappeared" in error_text
                )
                if not recoverable_failure:
                    raise
                if row_retry_count >= 2:
                    if (
                        review_identity and review_identity['Expedia Number']
                        and (
                            'panel disappeared while refreshing the virtual card' in error_text
                            or (
                                (card_unavailable_visible or review_identity)
                                and 'panel disappeared while reading the card status' in error_text
                            )
                        )
                    ):
                        review_note = (
                            'Expedia could not display the virtual card information after '
                            'refreshing; verify its status and amount manually.'
                        )
                        if review_identity['Reservation Canceled']:
                            review_note = 'Reservation is canceled; verify card charge eligibility. ' + review_note
                        if review_identity['Charge Window Passed']:
                            review_note += ' The 180-day charge window has passed.'
                        new_row = pd.DataFrame([{
                            **review_identity,
                            'Credit Card Number': '',
                            'Credit Card Expiry': '',
                            'Amount Due': None,
                            'Status': 'Virtual card unavailable',
                            'Needs Review': True,
                            'Review Note': review_note,
                        }])
                        data = pd.concat([data, new_row], ignore_index=True)
                        if checkpoint_path:
                            data.to_csv(checkpoint_path, index=False)
                        status_counts['Virtual card unavailable'] += 1
                        print(f"Review needed ({row_context}): {review_note}")
                        rows, row_number = restore_reservation_page(
                            driver, search_url, page_number, original_row_text, row_context,
                        )
                        row_number += 1
                        row_retry_count = 0
                        continue
                    raise RuntimeError(
                        f"Expedia could not recover the reservation details after two attempts "
                        f"({row_context}). Completed results are saved to the progress CSV."
                    ) from exc
                row_retry_count += 1
                print(
                    f"Expedia lost the reservation details at {row_context}; restoring the "
                    f"search and retrying that row (attempt {row_retry_count} of 2)."
                )
                rows, row_number = restore_reservation_page(
                    driver, search_url, page_number, original_row_text, row_context,
                )
                continue


            exit_button = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button')
            exit_button.click()
            # time.sleep(5)
            row_number += 1
            row_retry_count = 0
            if checkpoint_path:
                data.to_csv(checkpoint_path, index=False)
        try:
            # Wait until the 'Next' button is present and visible
            next_button = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, NEXT_PAGE_XPATH))
            )

            # Scroll the next_button into view
            driver.execute_script("arguments[0].scrollIntoView(true);", next_button)

            # Check if the 'Next' button is disabled
            is_disabled = "disabled" in next_button.get_attribute("class") or next_button.get_attribute("disabled") is not None

            if is_disabled:
                break
            else:
                old_first_text = rows[0].text
                ActionChains(driver).move_to_element(next_button).click().perform()
                WebDriverWait(driver, 25).until(
                    lambda browser: (
                        (visible_rows := browser.find_elements(
                            By.XPATH, f'{RESERVATION_TABLE_XPATH}/tr'
                        )) and visible_rows[0].text != old_first_text
                    )
                )
                page_number += 1
        except (NoSuchElementException, TimeoutException) as e:
            if debug:
                raise
            print(f"Element not found: {e}")
            pass

    if debug:
        print(f"\nCompleted scan: {start} to {end}")
        print(f"Reservation rows inspected: {sum(status_counts.values())}")
        for status, count in sorted(status_counts.items()):
            print(f"  {status}: {count}")
        review_rows = int(data['Needs Review'].fillna(False).sum()) if 'Needs Review' in data else 0
        report_rows_added = len(data) - initial_report_rows
        print(f"Report rows added: {report_rows_added} ({review_rows - initial_review_rows} flagged for review)")

    driver.get("https://apps.expediapartnercentral.com") # Go back to the main page
    return data

def send_email(data): # work in progress (include cases where the scraping fails for some reason based on the try except clauses below - eg Expedia website down or UI changes)
    sender_email = os.environ.get("EMAIL_ADDRESS")
    receiver_email = os.environ.get("RECEIVER_EMAIL")
    email_password = os.environ.get("EMAIL_PASSWORD") # NEED TO PUT APP PASSWORD IN ENV FILE
    smtp_server = "smtp.gmail.com"
    smtp_port = 587

    df_html = data.to_html(index=False, border=1)
    # customize the email based on the dates (first paragraph part in particular)
    html_body = f"""
    <html>
        <body>
            <p>Here is the Expedia Virtual Card data:</p>
            {df_html}
            <p>Best regards,<br>Your Raspberry Pi</p>
        </body>
    </html>
    """

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = "Expedia Virtual Card Report"

    msg.attach(MIMEText(html_body, "html"))

    try:
        # Send email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, email_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())

        print("Email sent successfully.")
    except Exception as e:
        print(f"Error sending email: {e}")

def main(debug: bool = False, manual_login: bool = False):
    # Manual login is a diagnostic mode: keep output visible and never email results.
    debug = debug or manual_login
    data = pd.DataFrame(columns=[
        'Check In Date', 'Check Out Date', 'Guest Name', 'Expedia Number',
        'Hotel Number', 'Credit Card Number', 'Credit Card Expiry', 'Amount Due',
        'Status', 'Charge Window Passed', 'Reservation Canceled', 'Needs Review', 'Review Note',
    ])
    load_dotenv(os.path.join('local-data', '.env'))
    prop_id = os.environ.get("PROP_ID")


    option = input("Choose an option (YTD, CUSTOM, L12M): ").strip().upper()
    
    # This is a temporary solution to handle the different date ranges
    if option == 'YTD':
        start_date = f"{datetime.now().year}-01-01"
        end_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    elif option == 'CUSTOM':
        start_date = input("Enter start date (YYYY-MM-DD): ").strip()
        end_date = input("Enter end date (YYYY-MM-DD): ").strip()
    elif option == 'L12M': # Last 12 months
        start_date = (datetime.now() - relativedelta(years=1)).strftime('%Y-%m-%d')
        end_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    # elif option == 'SINGLE': # single month might want to change this
    #     start_date = input("Enter start date (YYYY-MM-DD): ").strip()
    #     end_date = (datetime.strptime(start_date, '%Y-%m-%d') + timedelta(days=29)).strftime('%Y-%m-%d')
    else:
        print("Invalid option. Exiting.")
        return
    
    # Set up Chrome options for headless mode
    # Disable headless temporarily to debug
    chrome_options = Options()
    chrome_profile = os.path.abspath(os.path.join('local-data', 'chrome-profile'))
    os.makedirs(chrome_profile, exist_ok=True)
    chrome_options.add_argument(f'--user-data-dir={chrome_profile}')
    # chrome_options.add_argument("--headless")
    # chrome_options.add_argument("--disable-gpu")
    # chrome_options.add_argument("--window-size=1920,1080")
    # chrome_options.add_argument("--no-sandbox")
    # chrome_options.add_argument("--disable-dev-shm-usage")

    # Initialize the Chrome driver with the options
    # driver = webdriver.Chrome(service=Service("chromedriver.exe"), options=chrome_options)
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    driver.get("https://apps.expediapartnercentral.com")


    if manual_login:
        print("\nManual login: saved cookies and automatic credential entry are skipped.")
        print("Sign in yourself in this Chrome window and complete any verification there.")
    else:
        # Load cookies - assumes pickle file already exists
        with open(os.path.join('local-data', 'cookies.pkl'), "rb") as f:
            cookies = pickle.load(f)
        current_domain = driver.current_url.split("//")[1].split("/")[0]
        for cookie in cookies:
            if 'domain' not in cookie or cookie['domain'] in current_domain:
                driver.add_cookie(cookie)
        driver.refresh()
        time.sleep(2)

        # Reuse the authenticated Chrome profile when Expedia still trusts it.
        login_form_visible = any(
            element.is_displayed() for element in driver.find_elements(By.NAME, 'username')
        )
        if login_form_visible:
            user_name = os.environ.get("USER")
            password = os.environ.get("PASSWORD")
            submit_email_with_retry(driver, user_name)
            submit_password_with_retry(driver, password)
        else:
            print("No sign-in form is visible; continuing with the saved Chrome session.")

    if debug:
        # Let us observe sign-in before navigating away to the reservations page.
        if not manual_login:
            print("\nPassword submitted. Paused before opening reservations.")
            print("Watch Chrome finish signing in, and complete any verification in the browser.")
        print("If it returns to email or shows an error, leave this prompt waiting while you inspect it.")
        login_confirmed = input(
            "Once the Partner Central dashboard is visible, type CONTINUE (anything else stops): "
        ).strip().upper()
        if login_confirmed != "CONTINUE":
            print("Login was not confirmed. Stopping before opening reservations.")
            driver.quit()
            return
        save_auth_cookies(driver)
    else:
        time.sleep(2)
        save_auth_cookies(driver)

    os.makedirs('local-data', exist_ok=True)
    run_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    progress_csv_path = os.path.join('local-data', f'{run_stamp}_data_in_progress.csv')
    csv_path = os.path.join('local-data', f'{run_stamp}_data.csv')
    # Keep an explicitly marked partial report and update it after each row.
    data.to_csv(progress_csv_path, index=False)
    print(f"Progress report will be updated after each reservation: {progress_csv_path}")

    # Handle date splitting
    if (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days <= 31: # fix this
        print(f"Single month range: {start_date} to {end_date}")
        data = gather_data(
            data, driver, prop_id, start_date, end_date,
            debug=debug, checkpoint_path=progress_csv_path,
        )
    else:
        ranges = generate_monthly_date_ranges(start_date, end_date)
        for start, end in ranges:
            print(f"Fetching data from {start} to {end}")
            data = gather_data(
                data, driver, prop_id, start, end,
                debug=debug, checkpoint_path=progress_csv_path,
            )
            # data = pd.concat([data, new_rows], ignore_index=True)

    # Close the driver
    driver.quit()
    print(data)

    # Create CSV file if data is present
    data.to_csv(progress_csv_path, index=False)
    os.replace(progress_csv_path, csv_path)
    if len(data) > 0:
        review_rows = int(data['Needs Review'].fillna(False).sum()) if 'Needs Review' in data else 0
        print(f"Saved {len(data)} report rows ({review_rows} flagged for review) to {csv_path}")

        if debug:
            print("Debug mode: email skipped.")
        else:
            send_email(data)
    else:
        print(f"No reservations matched the report filters; an empty report was saved to {csv_path}")

def run_debug(manual_login: bool = False) -> int:
    """Show failures and pause so the browser can be inspected before exiting."""
    try:
        main(debug=True, manual_login=manual_login)
    except Exception:
        traceback.print_exc()
        try:
            input(
                "\nDebug run stopped. If Chrome is open, inspect it now.\n"
                "Press Enter when finished to exit: "
            )
        except (EOFError, KeyboardInterrupt):
            pass
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect Expedia virtual card reports.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show terminal output, pause after password submission, stop on scraping errors, and skip email.",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Sign in yourself without loading saved cookies. Enables debug mode and skips email.",
    )
    args = parser.parse_args()
    if args.debug or args.manual_login:
        print("Debug mode: output is shown here, scraping errors stop the run, and email is disabled.")
        sys.exit(run_debug(manual_login=args.manual_login))
    else:
        log_file = open(f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_output.log", "w")
        sys.stdout = log_file
        sys.stderr = log_file
        main()
        log_file.close()
