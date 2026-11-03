# Expedia Virtual Card Scraper

A Python and Selenium tool for auditing Expedia Partner Central virtual cards. It scans reservations for a selected property and date range, reads card statuses and payment details, and produces a CSV of outstanding balances and reservations needing review.

The script collects information for an audit. It does not charge cards, request replacement cards, or change reservations.

## Features

- Scan year to date, the last 12 months, or a custom date range using reservation check-in dates.
- Read the total payout from the separate **See payment details** page, including when that page uses iframes.
- Calculate remaining balances for partially charged cards.
- Check canceled reservations that still display a virtual card.
- Flag missing payment information and Expedia's expired charge-window warning for review.
- Save report progress while scanning, so collected results survive a later failure.
- Attempt recovery when Expedia cannot load reservations or the reservation details panel disappears.
- Reuse a local Chrome profile and saved cookies between runs.
- Email the report after a successful normal run; skip email during testing and manual login.

## Requirements

- Python 3.10 or newer.
- Google Chrome.
- An Expedia Partner Central account with access to the property's reservations and virtual cards.
- Internet access. `webdriver-manager` obtains the Chrome driver used by Selenium.

The instructions below use Windows PowerShell. Run commands from the project folder.

## Installation

```powershell
git clone https://github.com/jpatel207/expedia-virtual-card-scraper.git
cd expedia-virtual-card-scraper
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force local-data
```

If `py` is unavailable, use the command for your installed Python interpreter to create the virtual environment.

## Configuration

Create `local-data/.env` with your own settings:

```dotenv
USER=your_expedia_login_email
PASSWORD=your_expedia_password
PROP_ID=your_expedia_property_id
EMAIL_ADDRESS=your_sender_address@gmail.com
EMAIL_PASSWORD=your_gmail_app_password
RECEIVER_EMAIL=your_report_recipient@example.com
```

`PROP_ID` is the property's `htid` value in Partner Central URLs. `USER` and `PASSWORD` are used for automatic credential entry. The three email settings are used for normal runs that produce report rows; email is skipped in both testing modes below.

The script uses Gmail SMTP on port 587 with STARTTLS. Existing process environment variables take precedence over values loaded from `.env`.

## First run: manual login

Use manual login before trying automatic session reuse. It also creates the initial cookie backup required by the other run modes.

```powershell
.\.venv\Scripts\python.exe main.py --manual-login
```

1. Choose a date-range option in the terminal.
2. Sign in inside the Chrome window opened by the script. Complete SMS or other verification if Expedia requests it.
3. Wait until the Partner Central dashboard is visible.
4. Type `CONTINUE` at the terminal prompt to begin scanning.

After confirmation, cookies are saved to `local-data/cookies.pkl`. Chrome also keeps its session in `local-data/chrome-profile/`. Expedia can still require verification on later runs; saved sessions do not guarantee that verification will be skipped.

Close the previous script-controlled Chrome window before starting another run using the same profile.

## Run modes

| Command | Login behavior | Output | Email |
| --- | --- | --- | --- |
| `main.py --manual-login` | Sign in yourself; skips loading the cookie backup and automatic credential entry. Still uses the saved Chrome profile. | Visible terminal progress; pauses for inspection after an error. | Skipped |
| `main.py --debug` | Uses saved cookies/profile and attempts automatic sign-in when needed. Pauses for you to confirm the dashboard. | Visible terminal progress; pauses for inspection after an error. | Skipped |
| `main.py` | Uses saved cookies/profile and attempts automatic sign-in when needed. | Redirects console output to a timestamped `*_output.log` in the project folder. | Sends an HTML report if the scan finishes with report rows. |

For example, test saved-session login with:

```powershell
.\.venv\Scripts\python.exe main.py --debug
```

Run normally with:

```powershell
.\.venv\Scripts\python.exe main.py
```

All modes currently open a visible Chrome window. Use manual login or debug mode when sign-in needs your attention. In normal mode, the date-range prompts are also redirected to the log file, so check that file if the terminal appears to be waiting without displaying a prompt.

### Date-range options

- **YTD:** January 1 of the current year through yesterday.
- **CUSTOM:** Enter start and end dates in `YYYY-MM-DD` format.
- **L12M:** The date one year before today through yesterday.

Longer ranges are split into monthly scans. Start with a short custom range when checking a UI change or login problem.

## Reports and balance calculations

The CSV includes check-in/check-out dates, guest name, Expedia reservation number, hotel confirmation number, virtual card number and expiry, card status, amount due, and review fields.

**Amount Due** is calculated as follows:

- **Ready to charge:** the **Your total payout** amount from the payment-details tab.
- **Partially charged:** that payout minus the **You've charged** amount in the reservation panel, rounded to two decimal places.
- If the necessary amounts cannot be read reliably, the balance is left blank and the reservation is flagged for review.

The report is filtered rather than being a complete export of all reservations:

- Cards marked **Charged in full** are excluded.
- **Ready to charge** cards are included.
- Partially charged cards with a known balance of **$5 or more** are included.
- Review flags can include partially charged cards below $5, such as those showing an expired charge-window warning.
- Canceled reservations with a displayed virtual card are included when the card is not marked **Charged in full**, so cancellation-related balances can be reviewed.
- **Refund due** and **Deactivated** cards are otherwise excluded.

`Needs Review` and `Review Note` explain cases such as unreadable amounts, incomplete card details, canceled reservations, or an expired charge window. A reported balance does not establish that a card is currently chargeable; review Expedia's status and eligibility information.

When Expedia still cannot display a virtual card after **Refresh card**, the report records that reservation as **Virtual card unavailable** with a blank amount and card details. It remains flagged for manual review; the script does not infer a chargeable balance.

## Progress saving and recovery

At the beginning of a scan, the script creates:

```text
local-data/YYYY-MM-DD_HH-MM-SS_data_in_progress.csv
```

It updates that report as reservations are processed. If the run stops, the file retains the report rows already collected, including rows from earlier months. It contains qualifying report rows, not a record of every reservation inspected; its latest check-in date may be earlier than the month currently being scanned.

After the scan reaches completion, the file is renamed to:

```text
local-data/YYYY-MM-DD_HH-MM-SS_data.csv
```

You can view an in-progress CSV in VS Code while the script runs. Avoid editing or saving it, and reopen it if the displayed contents appear stale.

The script can click a visible virtual-card reload control when a card fails to load. If Expedia shows **reservations cannot be loaded**, or the details panel disappears, it attempts to reload the same search, return to the recorded page, and retry the failed reservation up to twice. It matches the failed row using its visible text and stops if it cannot identify it uniquely. When card information remains unavailable, it keeps the reservation in the report for review and continues scanning.

**Restarting the program does not resume from an in-progress CSV.** Choose a new custom range to rescan the unfinished month, or rerun the original range for a single complete report. Each run creates a separate file. Failed runs do not email partial results.

## Local files and Git

The repository's `.gitignore` excludes `local-data/`, `.venv/`, and root-level `*_data.csv` files. Keep credentials, reports, cookies, Chrome session data, and screenshots in `local-data/`.

Reports contain guest and payment-card information, and the Chrome profile and cookies contain session information. Keep these files private. Normal runs currently write `*_output.log` files in the project folder; those logs are not covered by the current ignore rules, so review them before staging files for Git.

## Troubleshooting and maintenance

- **First run fails because `cookies.pkl` is missing:** run with `--manual-login` to establish a session and save cookies.
- **Expedia asks for SMS verification:** use `--manual-login` or `--debug`, complete verification in Chrome, then confirm with `CONTINUE`.
- **Chrome cannot open the profile:** close the previous script-controlled Chrome window and exit its Python process before retrying.
- **A debug run stops:** inspect Chrome before pressing Enter to exit. The partial CSV remains available, but refreshing Chrome manually does not resume the stopped Python code.
- **No email arrives:** debug/manual-login runs skip email, and empty or failed scans do not send a report. For a completed normal run with report rows, check its log for SMTP errors and verify the email settings.
- **Expedia changes its UI:** some fields and controls still use positional XPath selectors. Review the affected selector and test a short range with `--debug` or `--manual-login` before running a larger audit.

This project is an independent browser-automation tool and is not affiliated with Expedia Group.
