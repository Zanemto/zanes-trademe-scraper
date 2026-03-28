# TradeMe Car Deal Scraper

Scrapes new TradeMe listings, scores them against historical prices,
and emails the top 5 deals from the latest scrape.

Edit the filters at the top of `scraper.py`.

---

## Setup

### 1. Install dependencies

```bash
pip install playwright
playwright install chromium
```

### 2. Configure your email (Gmail recommended)

Gmail requires an **App Password** (your real Gmail password will not work
because of 2FA). Here is how to get one:

1. Go to your Google Account -> **Security**
2. Under "How you sign in to Google", click **2-Step Verification** (enable it if not already)
3. Scroll to the bottom -> **App passwords**
4. Create a new app password (name it "TradeMe Scraper")
5. Copy the 16-character password Google gives you

Rename `config.example.py` to `config.py`, then edit it:

```python
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

SMTP_USER = "example@gmail.com"
SMTP_PASS = "abcd efgv sfdc pohg"   # app password (spaces are fine)

EMAIL_FROM = "example@gmail.com"
EMAIL_TO   = "example@gmail.com"    # where to receive alerts
```

### 3. Test it manually

```bash
# Just scrape and print results (no email)
python scraper.py

# Send email (after scraping)
python mailer.py
```

---

## Run it every day automatically

### Mac / Linux (cron)

Open your crontab:
```bash
crontab -e
```

Add these lines to run every morning at 8am:
```
0 8 * * * cd /path/to/trademe_scraper && python scraper.py >> scraper.log 2>&1
5 8 * * * cd /path/to/trademe_scraper && python mailer.py >> mailer.log 2>&1
```

Replace `/path/to/trademe_scraper` with the actual folder path
(e.g. `/Users/yourname/trademe_scraper`).

### Windows (Task Scheduler)

1. Open **Task Scheduler** -> Create Basic Task
2. Set trigger: **Daily** at 8:00 AM
3. Action: **Start a program** (create two tasks, 5 minutes apart)
   - Program: `python`
   - Arguments: `scraper.py` (first task)
   - Start in: `C:\path\to\trademe_scraper`
4. Second task:
   - Program: `python`
   - Arguments: `mailer.py` (second task)
   - Start in: `C:\path\to\trademe_scraper`

---

## How the deal scoring works

Each new listing is compared to the **median price** of similar cars
(same make, same model, year within ±3 years) already in the database.
The score is **price as a percentage of the median** (lower is better).

| Score | Meaning |
|-------|---------|
| ≤ 80% | Hot deal - priced well below median |
| ≤ 90% | Good deal |
| 90–110% | Near median |
| > 110% | Above median |

**Note:** Scoring improves over time as more listings accumulate in the database.

---

## Files

| File | Purpose |
|------|---------|
| `scraper.py` | Playwright scraper + DB + deal scorer |
| `mailer.py` | Emails the top 5 deals from the latest scrape |
| `config.py` | Your email credentials (local only) |
| `config.example.py` | Template config to copy |
| `trademe_cars.db` | SQLite database (created automatically) |
| `db_view.py` | View recent listings from the database |
| `scraper.log` | Cron output log |
| `mailer.log` | Cron output log |

---

## Troubleshooting

**No listings found / 0 cards scraped**
TradeMe may have updated their HTML structure. Open `scraper.py` and
update the CSS selectors in `scrape_page()`. Use Playwright's
`page.pause()` in headed mode to inspect the live DOM:
```python
browser = await p.chromium.launch(headless=False)  # see the browser
await page.pause()                                  # opens inspector
```

**Blocked / Cloudflare challenge**
- Increase the sleep delays in `scrape_all_pages()`
- Run at off-peak hours (the cron runs at 8am by default - try 3am)
- Reduce `MAX_PAGES` to scrape less

**Email not sending**
- Double-check your App Password in `config.py`
- Make sure 2FA is enabled on your Google account
- Check `mailer.log` for the full error
