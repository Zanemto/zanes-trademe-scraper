# Zanes TradeMe Car Deal Scraper

Scrapes new TradeMe car listings, scores them against historical prices in a local database, and emails you the best deals. Comes with a web UI to manage everything from the browser.

---

## Setup

### 1. Install dependencies

```bash
pip install playwright apscheduler waitress flask
python -m playwright install chromium
```

### 2. Configure your email (Gmail recommended)

Gmail requires an **App Password** (your real Gmail password will not work because of 2FA). Here is how to get one:

1. Go to your Google Account → **Security**
2. Under "How you sign in to Google", click **2-Step Verification** (enable it if not already)
3. Scroll to the bottom → **App passwords**
4. Create a new app password (name it "TradeMe Scraper")
5. Copy the 16-character password Google gives you

Rename `config.example.py` to `config.py`, then fill it in:

```python
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

SMTP_USER = "example@gmail.com"
SMTP_PASS = "abcd efgv sfdc pohg"   # app password (spaces are fine)

EMAIL_FROM = "example@gmail.com"
EMAIL_TO   = "example@gmail.com"    # where to receive alerts
```

### 3. Start the UI

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

---

## Web UI

The UI has three pages:

### Dashboard
- Shows total listings, scraped today, and scraped in the last 7 days
- **Run Scraper** — manually trigger a scrape right now
- **Run Mailer** — manually send the email report right now
- **Automatic Schedule** — set a daily time for the scraper and mailer to run automatically (see below)

For both manual runs and the schedule, you can choose which filter sets to include using the checkboxes.

### Listings
A searchable, paginated table of everything in the database — title, price, kilometres, year, make, model, listing type, region, and date scraped. Each title links to the original TradeMe listing.

### Filters
Two tabs — **Scraper Filters** and **Mailer Filters** — where you can add, edit, and remove filter sets without touching any code. Changes are saved immediately to `scraper_filters.json` and `mailer_filters.json`.

---

## Automatic Scheduling

On the Dashboard under **Automatic Schedule**, you can enable a daily run time for the scraper and mailer independently. The schedule is stored in `schedule_config.json` and applied when the app starts.

> **Note:** The schedule only runs while `app.py` is running. If you restart the app, the saved schedule is automatically re-applied.

The last scheduled run result (time, success/failure, and output) is shown on the dashboard.

---

## Filter sets

### Scraper filters (`scraper_filters.json`)

Controls what the scraper searches for on TradeMe. Each filter set is one TradeMe search URL. All results are saved to the database.

| Field | Description |
|-------|-------------|
| `name` | Label shown in the UI |
| `make` | TradeMe URL slug, e.g. `mercedes-benz`, `toyota` (blank = any) |
| `model` | TradeMe URL slug, e.g. `c-200`, `corolla` (blank = any) |
| `year_min` | Minimum year |
| `min_price` / `max_price` | Price range |
| `max_kms` | Maximum odometer |
| `region_id` | TradeMe region ID (blank = all NZ) |
| `classifieds` | `true` = classifieds only, `false` = auctions only, blank = both |
| `max_pages` | How many pages to scrape per filter set (~20 listings per page) |

> **Make vs model format:** The scraper `make` and `model` fields go directly into the TradeMe URL path, so they must use TradeMe's slug format (lowercase, hyphens). For example, `mercedes-benz` not `Mercedes`.

### Mailer filters (`mailer_filters.json`)

Controls which listings from the database are included in each email report. Each filter set generates one email.

| Field | Description |
|-------|-------------|
| `name` | Label shown in the UI and email subject |
| `make` | Matched case-insensitively against extracted make, e.g. `Mercedes` |
| `model` | Single model or comma-separated list, e.g. `C 200, C 180` |
| `year_min` | Minimum year |
| `min_price` / `max_price` | Price range |
| `max_kms` | Maximum odometer |
| `region_id` | TradeMe region ID (blank = all) |
| `classifieds` | `true` = classifieds only, `false` = auctions only, blank = both |
| `year_window` | Compare against cars within ±this many years (default 2) |
| `km_window` | Compare against cars within ±this many km (blank = ignore) |
| `days_back` | Only email listings scraped within this many days |

> **Make format difference:** The mailer `make` is matched against what the scraper extracted from the listing title (e.g. `Mercedes`, not `mercedes-benz`).

---

## How deal scoring works

Each listing is compared to the **median price** of similar cars already in the database (same make, same model, year within ±`year_window`, optionally kilometres within ±`km_window`). If fewer than 3 comparable listings exist, it falls back to the overall database median.

The score is **price as a percentage of the median** — lower is better.

| Badge | Score | Meaning |
|-------|-------|---------|
| Hot deal | ≤ 80% | Priced well below median |
| Good deal | ≤ 90% | Below median |
| New listing | > 90% | At or above median |

Scoring improves over time as more listings accumulate in the database.

---

## Running without the UI

You can also run the scraper and mailer directly from the terminal:

```bash
# Scrape all filter sets
python scraper.py

# Scrape specific filter sets by name
python scraper.py --filters "Mercedes C Class,Toyota Corolla"

# Send email for all mailer filter sets
python mailer.py

# Send email for specific filter sets
python mailer.py --filters "Mercedes C Class"
```

---

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask web UI + APScheduler |
| `scraper.py` | Playwright scraper + SQLite storage |
| `mailer.py` | Deal scorer + HTML email sender |
| `config.py` | Your email credentials (local only, not committed) |
| `config.example.py` | Template config to copy |
| `scraper_filters.json` | Scraper filter sets and max pages setting |
| `mailer_filters.json` | Mailer filter sets |
| `schedule_config.json` | Saved automatic schedule |
| `trademe_cars.db` | SQLite database (created automatically) |
| `db_view.py` | CLI tool to view recent listings from the database |
| `templates/` | HTML templates for the web UI |

---

## Troubleshooting

**No listings found / 0 cards scraped**
TradeMe may have updated their HTML. To debug, temporarily switch the scraper to headed mode and pause it:
```python
browser = await p.chromium.launch(headless=False)
await page.pause()  # opens the Playwright inspector
```

**Blocked / Cloudflare challenge**
- Increase the sleep delays in `scrape_all_pages()` in `scraper.py`
- Reduce `max_pages` in the UI
- Try scheduling the scrape at an off-peak time like 3am

**Email not sending**
- Double-check your App Password in `config.py`
- Make sure 2FA is enabled on your Google account
