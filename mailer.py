"""
TradeMe Car Deal Mailer
Runs the scraper then emails you the best new deals.
Configure your email settings in config.py before running.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from scraper import init_db, score_deals
import config

# ── Mail filter sets (edit these to control which emails you receive) ─────────
# Each entry generates one email. make/model are matched case-insensitively
# against what was extracted from listing titles — e.g. "Mercedes", not "mercedes-benz".
MAIL_FILTER_SETS = [
    {
        "name": "Mercedes C200",
        "make": "Mercedes",
        "model": None,
        "year_min": 2010,
        "min_price": None,
        "max_price": 15000,
        "max_kms": 140_000,
    },
]
# ─────────────────────────────────────────────────────────────────────────────


def build_html_email(deals: list[dict], fset: dict) -> str:
    """Build a clean HTML email body from the scored deals list."""
    def build_filter_text() -> str:
        parts = [f"make {fset.get('make')}"]
        if fset.get("model"):
            parts.append(f"model {fset.get('model')}")
        if fset.get("year_min") is not None:
            parts.append(f"year ≥ {fset.get('year_min')}")
        min_price = fset.get("min_price")
        max_price = fset.get("max_price")
        if min_price is not None or max_price is not None:
            if min_price is not None and max_price is not None:
                parts.append(f"price ${min_price:,}–${max_price:,}")
            elif min_price is not None:
                parts.append(f"price ≥ ${min_price:,}")
            else:
                parts.append(f"price ≤ ${max_price:,}")
        if fset.get("max_kms") is not None:
            parts.append(f"max {fset.get('max_kms'):,} km")
        if fset.get("region_id") is not None:
            parts.append(f"region id {fset.get('region_id')}")
        parts.append("newest first")

        label = fset.get("name") or fset.get("make") or "Filter set"
        return label + ": " + " · ".join(parts)

    def deal_badge(pct: float) -> str:
        # pct is price as % of median (lower is better)
        if pct <= 80:
            return '<span style="background:#16a34a;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold;">Hot deal</span>'
        if pct <= 90:
            return '<span style="background:#ca8a04;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold;">Good deal</span>'
        return '<span style="background:#6b7280;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold;">New listing</span>'

    rows = ""
    for car in deals[:10]:   # cap at 10 listings per email
        kms = f"{car['kilometres']:,} km" if car.get("kilometres") else "km unknown"
        saving_color = "#16a34a" if car["saving_pct"] <= 90 else "#374151"
        saving_text  = f"{car['saving_pct']:.0f}% of median ${car['median_comp']:,}"

        rows += f"""
        <tr>
          <td style="padding:16px;border-bottom:1px solid #e5e7eb;vertical-align:top;">
            <div style="font-weight:600;font-size:15px;margin-bottom:4px;">{car['title']}</div>
            <div style="color:#6b7280;font-size:13px;margin-bottom:8px;">{kms}</div>
            {deal_badge(car['saving_pct'])}
          </td>
          <td style="padding:16px;border-bottom:1px solid #e5e7eb;vertical-align:top;text-align:right;white-space:nowrap;">
            <div style="font-size:20px;font-weight:700;">${car['price']:,}</div>
            <div style="color:{saving_color};font-size:12px;margin-top:4px;">{saving_text}</div>
            <a href="{car['url']}" style="display:inline-block;margin-top:8px;background:#3b82f6;color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px;">View →</a>
          </td>
        </tr>
        """

    date_str = datetime.now().strftime("%A %d %B %Y")
    total    = len(deals)
    hot      = sum(1 for d in deals if d["saving_pct"] <= 80)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:24px;">
      <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">

        <!-- Header -->
        <div style="background:#1e3a5f;padding:28px 32px;">
          <h1 style="color:#fff;margin:0;font-size:22px;">TradeMe Deal Report — {fset.get("name") or "All"}</h1>
          <p style="color:#93c5fd;margin:6px 0 0;font-size:14px;">{date_str}</p>
        </div>

        <!-- Summary bar -->
        <div style="background:#eff6ff;padding:16px 32px;border-bottom:1px solid #dbeafe;">
          <div style="display:inline-block;margin-right:28px;">
            <span style="font-size:24px;font-weight:700;color:#1e40af;">{total}</span>
            <span style="color:#6b7280;font-size:13px;margin-left:6px;">new listings</span>
          </div>
          <div style="display:inline-block;">
            <span style="font-size:24px;font-weight:700;color:#16a34a;">{hot}</span>
            <span style="color:#6b7280;font-size:13px;margin-left:6px;">hot deals (≤80% of median)</span>
          </div>
        </div>

        <!-- Listings table -->
        <table style="width:100%;border-collapse:collapse;">
          {rows}
        </table>

        <!-- Footer -->
        <div style="padding:20px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;">
          <p style="color:#9ca3af;font-size:12px;margin:0;">
            Filters: {build_filter_text()}<br>
            Deal scores compare against median price of similar cars in your local database.
            Scores improve as more data is collected over time.
          </p>
        </div>

      </div>
    </body>
    </html>
    """
    return html


def send_email(html_body: str, num_deals: int, filter_name: str = ""):
    label = f" — {filter_name}" if filter_name else ""
    subject = f"TradeMe: {num_deals} new car deals{label} — {datetime.now().strftime('%d %b')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_FROM
    msg["To"]      = config.EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    print(f"Sending email to {config.EMAIL_TO}...")
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.login(config.SMTP_USER, config.SMTP_PASS)
        server.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_string())
    print("Email sent!")


def fetch_latest_listings(con) -> list[dict]:
    """Fetch all listings scraped within the last 3 days."""
    since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    rows = con.execute(
        """
        SELECT listing_id, title, price, kilometres, year, make, model, url, date_scraped
        FROM listings
        WHERE date_scraped >= ?
        """,
        (since,),
    ).fetchall()

    listings = []
    for r in rows:
        listing_id, title, price, kilometres, year, make, model, url, date_scraped = r
        listings.append({
            "listing_id": listing_id,
            "title": title,
            "price": price,
            "kilometres": kilometres,
            "year": year,
            "make": make,
            "model": model,
            "url": url,
            "date_scraped": date_scraped,
        })
    return listings


def apply_filter(listings: list[dict], fset: dict) -> list[dict]:
    """Return listings that match the given mail filter set criteria."""
    results = []
    for car in listings:
        make = fset.get("make")
        if make and make.lower() not in (car.get("make") or "").lower():
            continue
        model = fset.get("model")
        if model and model.lower() not in (car.get("model") or "").lower():
            continue
        year_min = fset.get("year_min")
        if year_min and car.get("year") and car["year"] < year_min:
            continue
        min_price = fset.get("min_price")
        if min_price and car.get("price") and car["price"] < min_price:
            continue
        max_price = fset.get("max_price")
        if max_price and car.get("price") and car["price"] > max_price:
            continue
        max_kms = fset.get("max_kms")
        if max_kms and car.get("kilometres") and car["kilometres"] > max_kms:
            continue
        results.append(car)
    return results


def main():
    con = init_db()
    latest_listings = fetch_latest_listings(con)
    if not latest_listings:
        print("No listings found from the most recent scraper run.")
        con.close()
        return

    for fset in MAIL_FILTER_SETS:
        filter_name = fset.get("name", "")
        matched = apply_filter(latest_listings, fset)
        if not matched:
            print(f"No new listings for '{filter_name}'.")
            continue

        deals = score_deals(con, matched)
        if not deals:
            print(f"No scoreable deals for '{filter_name}'.")
            continue

        top_deals = deals[:10]
        html = build_html_email(top_deals, fset)
        send_email(html, len(top_deals), filter_name)

    con.close()


if __name__ == "__main__":
    main()
