import json
import base64
import pandas as pd
import os
import pickle
import time
import re
import smtplib
import sys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from webdriver_manager.chrome import ChromeDriverManager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

log_file = open(f"{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}_output.log", "w")
sys.stdout = log_file
sys.stderr = log_file

def check_exists_by_xpath(xpath: str, webdriver: webdriver.Chrome) -> bool:
    """Check if an element exists by XPATH. Currently unused, but may be useful in the future and cutdown gather_data codeblock."""
    try:
        webdriver.find_element_by_xpath(xpath)
    except NoSuchElementException:
        return False
    return True


def extract_float_from_string(s: str) -> float:
    """Extract float value from a string that may contain commas."""
    match = re.search(r'[\d,]+\.\d+', s)
    if match:
        return float(match.group().replace(',', ''))
    return 0.0

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

def gather_data(data: pd.DataFrame, driver: webdriver.Chrome, prop_id: str, start: str, end: str) -> pd.DataFrame:
    """Gather data from the reservation table."""
    payload = build_payload(start, end)
    # print(f"Payload: {payload}")

    driver.get(f"https://apps.expediapartnercentral.com/lodging/bookings?htid={prop_id}#{payload}")
    time.sleep(20)

    # Runs until the 'Next' button is disabled
    while True:
        table = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, '//*[@id="content"]/div/div[3]/div/div/div[2]/div/div/div[2]/table/tbody')))

        # Get all rows in the table (excluding header row)
        rows = WebDriverWait(table, 25).until(
            EC.presence_of_all_elements_located((By.TAG_NAME, 'tr'))
        )
        # print(rows)

        # Loop through each row and interact with the button
        for row in rows:
            # Find the button within the row
            try:
                guest_name_button = row.find_element(By.CLASS_NAME, 'fds-button2.link.guestNameLink')
                # guest_name_button = WebDriverWait(row, 25).until(
                #     EC.presence_of_element_located((By.CLASS_NAME, 'fds-button2.link.guestNameLink'))
                # )
                driver.execute_script("arguments[0].click();", guest_name_button)
                # guest_name_button.click()
                time.sleep(10)
            
            # try:
                status_element = WebDriverWait(driver, 20).until(
                    EC.visibility_of_element_located((By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[1]/span'))
                )

                status_text = status_element.text.strip()

                # Determine the correct XPath for amount_due based on the status text
                if status_text == 'Charged in full' or status_text == 'Refund due' or status_text == 'Deactivated': # Should take into consideration when front desk overcharges card, perhaps a different script?
                    amount_due = 0
                else:
                    # if check_exists_by_xpath('//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[4]/div[2]', driver):
                    try:
                        # Check if the specific element is present
                        past_180_days_indicator = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[4]/div[2]')
                        total_payout_element = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[4]/div/div[2]/div[1]/div[3]/div/div[2]/div/div/span/span[2]')
                        total_payout = float(total_payout_element.text.split('USD')[0].strip().replace(',', ''))

                        try:
                            charge_amount_element = driver.find_element(By.XPATH, '//* [@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[2]/div').text
                            charge_amount = extract_float_from_string(charge_amount_element)
                        except NoSuchElementException as e:
                            # Element is not found, perform alternative action
                            print(f"Element not found: {e}")
                            charge_amount = -1

                        if status_text == 'Partially charged':
                            amount_due = round(total_payout - charge_amount, 2) # The total_payout amount doesn't actually match what hotel should charge
                        elif status_text == 'Ready to charge' and charge_amount != -1:
                            amount_due = round(charge_amount, 2)
                        elif status_text == 'Ready to charge' and charge_amount == -1:
                            amount_due = round(total_payout, 2)


                    except NoSuchElementException:
                    # else:
                        # Element is not found, perform alternative action
                        print("Past 180 element not found. Moving on to post 180 day search.")

                        if status_text == 'Partially charged':
                            try:
                                amount_due_xpath = '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[4]/div/span/span[2]' # Remaining amount to charge number field
                                amount_due = round(float(driver.find_element(By.XPATH, amount_due_xpath).text.strip()), 2)
                            except NoSuchElementException as e:
                                print(f"Element not found (inner partially charged element check): {e}")
                                print("no amount_due normal field")
                                amount_due_xpath = '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[6]/div/span/span[2]' # Remaining amount to charge when there is an error field
                                amount_due = round(float(driver.find_element(By.XPATH, amount_due_xpath).text.strip()), 2)
                        else:
                            amount_due_xpath = '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[3]/div/span/span[2]'
                            amount_due = round(float(driver.find_element(By.XPATH, amount_due_xpath).text.strip()), 2)
                        # amount_due = round(float(driver.find_element(By.XPATH, amount_due_xpath).text.strip()), 2)
                    
                check_in_date_str, check_out_date_str = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[2]/div[1]/div/div[1]/div').text.split('\n')[0].split(' - ')
                
                # Printing all rows for debugging only, remove after done
                check_in_date = datetime.strptime(check_in_date_str, '%m/%d/%Y')
                check_out_date = datetime.strptime(check_out_date_str, '%m/%d/%Y')
                guest_name = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[1]/div[1]/div[2]/div').text.strip()
                expedia_number = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[2]/div/div[1]/header/h3/span/span').text.strip().lstrip('#')
                cc_number = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[2]').text.strip()
                cc_exp = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/div/div/div[3]/div/div[2]/div/div[1]/div/div[2]/div[4]/div[1]').text.strip()
                print(f"Check In Date: {check_in_date}, Check Out Date: {check_out_date}, Guest Name: {guest_name}, Expedia Number: {expedia_number}, Credit Card Number: {cc_number}, Credit Card Expiry: {cc_exp}, Amount Due: {amount_due}, 'Status': {status_text}")

                if (status_text == 'Ready to charge') or (status_text == 'Partially charged' and amount_due >= 5):
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
                        'Amount Due': amount_due
                    }])

                    # Append the new row to the existing DataFrame
                    data = pd.concat([data, new_row], ignore_index=True)

                # exit_button = driver.find_element(By.XPATH, '//*[@id="sidePanelDialog"]/div/div/header/button')
                # exit_button.click()
                # time.sleep(5)


            except (NoSuchElementException, TimeoutException) as e:
                print(f"Element not found (main try-except): {e}")
                driver.save_screenshot(f'screenshots/{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}_error_screenshot.png')
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
        except (NoSuchElementException, TimeoutException) as e:
            print(f"Element not found: {e}")
            pass

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

def main():
    data = pd.DataFrame(columns=['Check In Date', 'Check Out Date', 'Guest Name', 'Expedia Number', 'Hotel Number', 'Credit Card Number', 'Credit Card Expiry', 'Amount Due'])
    load_dotenv(r".gitignore\.env")
    user_name = os.environ.get("USER")
    password = os.environ.get("PASSWORD")
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


    # Load cookies - assumes pickle file already exists
    with open(r".gitignore\cookies.pkl", "rb") as f:
        cookies = pickle.load(f)
    current_domain = driver.current_url.split("//")[1].split("/")[0]
    for cookie in cookies:
        if 'domain' not in cookie or cookie['domain'] in current_domain:
            driver.add_cookie(cookie)
    
    # Log in
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
    time.sleep(2)

    # Handle date splitting
    if (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days <= 31: # fix this
        print(f"Single month range: {start_date} to {end_date}")
        new_rows = gather_data(data, driver, prop_id, start_date, end_date)
        data = pd.concat([data, new_rows], ignore_index=True)
    else:
        ranges = generate_monthly_date_ranges(start_date, end_date)
        for start, end in ranges:
            print(f"Fetching data from {start} to {end}")
            data = gather_data(data, driver, prop_id, start, end)
            # data = pd.concat([data, new_rows], ignore_index=True)

    # Close the driver
    driver.quit()
    print(data)

    # Create CSV file if data is present
    if len(data) > 0:
        data.to_csv(f'{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}_data.csv', index=False)

        send_email(data)

if __name__ == "__main__":
    main()
    log_file.close()