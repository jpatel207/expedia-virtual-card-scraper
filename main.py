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
from selenium.webdriver.common.keys import Keys
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


def read_reservation_status(driver, row_context: str) -> tuple[str, str]:
    """Read the status text from the rendered reservation panel."""
    known_statuses = (
        'Charged in full', 'Partially charged', 'Ready to charge',
        'Refund due', 'Deactivated', 'Canceled', 'Cancelled',
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
        return False

    try:
        return WebDriverWait(driver, 20).until(find_status)
    except TimeoutException as exc:
        raise TimeoutException(
            f"Timed out while reading a recognized reservation status ({row_context})."
        ) from exc


def gather_data(data: pd.DataFrame, driver: webdriver.Chrome, prop_id: str, start: str, end: str, debug: bool = False) -> pd.DataFrame:
    """Gather data from the reservation table."""
    status_counts = Counter()
    initial_report_rows = len(data)
    initial_review_rows = int(data['Needs Review'].fillna(False).sum()) if 'Needs Review' in data else 0
    payload = build_payload(start, end)
    # print(f"Payload: {payload}")

    driver.get(f"https://apps.expediapartnercentral.com/lodging/bookings?htid={prop_id}#{payload}")
    time.sleep(20)

    # Runs until the 'Next' button is disabled
    page_number = 1
    while True:
        scan_context = f"{start} to {end}, page {page_number}"
        table = wait_for_reservation_element(
            driver,
            (By.XPATH, '//*[@id="content"]/div/div[3]/div/div/div[2]/div/div/div[2]/table/tbody'),
            f"loading the reservations table ({scan_context})",
        )

        # Get all rows in the table (excluding header row)
        rows = WebDriverWait(table, 25).until(
            EC.presence_of_all_elements_located((By.TAG_NAME, 'tr'))
        )
        # print(rows)

        # Loop through each row and interact with the button
        for row_number, row in enumerate(rows, start=1):
            row_context = f"{scan_context}, row {row_number} of {len(rows)}"
            if debug:
                print(f"Opening reservation details: {row_context}")
            # Find the button within the row
            try:
                guest_name_button = row.find_element(By.CLASS_NAME, 'fds-button2.link.guestNameLink')
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
                if debug:
                    print(f"Details panel opened; waiting for card status: {row_context}")
                status_text, panel_text = read_reservation_status(driver, row_context)
                status_counts[status_text] += 1
                reservation_cancelled = any(
                    line.strip().casefold() in {'canceled', 'cancelled'}
                    for line in panel_text.splitlines()
                )

                if status_text in {'Canceled', 'Cancelled'}:
                    if 'virtual card' not in panel_text.casefold():
                        if debug:
                            print(f"Skipping canceled reservation without a virtual card: {row_context}")
                        driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button').click()
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
                cc_number = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[2]').text.strip()
                cc_exp = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[4]/div[1]').text.strip()
                if debug and payment['Needs Review']:
                    print(f"Review needed ({row_context}): {payment['Review Note']}")

                if should_include_reservation(status_text, payment):
                    check_in_date_str, check_out_date_str = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[2]/div[1]/div/div[1]/div').text.split('\n')[0].split(' - ')
                    # Convert the date strings to datetime objects
                    check_in_date = datetime.strptime(check_in_date_str, '%m/%d/%Y')
                    check_out_date = datetime.strptime(check_out_date_str, '%m/%d/%Y')
                    guest_name = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[1]/div[1]/div[2]/div').text.strip()
                    expedia_number = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[1]/header/h3/span/span').text.strip().lstrip('#')
                    cc_number = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[2]').text.strip()
                    cc_exp = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[4]/div[1]').text.strip()
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


            exit_button = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button')
            exit_button.click()
            # time.sleep(5)
        try:
            # Wait until the 'Next' button is present and visible
            next_button = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="content"]/div/div[3]/div/div/div[2]/div/nav/div/div[2]/button'))
            )

            # Scroll the next_button into view
            driver.execute_script("arguments[0].scrollIntoView(true);", next_button)

            # Check if the 'Next' button is disabled
            is_disabled = "disabled" in next_button.get_attribute("class") or next_button.get_attribute("disabled") is not None

            if is_disabled:
                break
            else:
                ActionChains(driver).move_to_element(next_button).click().perform()
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

        # Log in
        user_name = os.environ.get("USER")
        password = os.environ.get("PASSWORD")
        user_input = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.NAME, "username"))
        )
        user_input.send_keys(user_name)
        user_input.send_keys(Keys.RETURN)
        password_input = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.NAME, "password"))
        )
        password_input.send_keys(password)
        password_input.send_keys(Keys.RETURN)

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
    else:
        time.sleep(2)

    # Handle date splitting
    if (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days <= 31: # fix this
        print(f"Single month range: {start_date} to {end_date}")
        new_rows = gather_data(data, driver, prop_id, start_date, end_date, debug=debug)
        data = pd.concat([data, new_rows], ignore_index=True)
    else:
        ranges = generate_monthly_date_ranges(start_date, end_date)
        for start, end in ranges:
            print(f"Fetching data from {start} to {end}")
            data = gather_data(data, driver, prop_id, start, end, debug=debug)
            # data = pd.concat([data, new_rows], ignore_index=True)

    # Close the driver
    driver.quit()
    print(data)

    # Create CSV file if data is present
    if len(data) > 0:
        os.makedirs('local-data', exist_ok=True)
        csv_path = os.path.join(
            'local-data', f'{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}_data.csv'
        )
        data.to_csv(csv_path, index=False)
        review_rows = int(data['Needs Review'].fillna(False).sum()) if 'Needs Review' in data else 0
        print(f"Saved {len(data)} report rows ({review_rows} flagged for review) to {csv_path}")

        if debug:
            print("Debug mode: email skipped.")
        else:
            send_email(data)
    else:
        print("No reservations matched the report filters; no CSV was created.")

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
