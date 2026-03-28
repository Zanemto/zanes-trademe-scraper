"""
TradeMe Car Scraper
Scrapes newly listed cars from TradeMe matching your filters,
scores them for value, and emails you the best deals when you run mailer.py.
"""

import asyncio
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ── Filters (edit these to change your search) ──────────────────────────────
MAKE      = "toyota"
YEAR_MIN  = 2005
MIN_PRICE = 1000
MAX_PRICE = 7500
MAX_KMS   = 180_000
REGION_ID = 2               # TradeMe "user_region" id for Auckland
MAX_PAGES = 5
# ────────────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).with_name("trademe_cars.db")


# ── Database setup ────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            listing_id   TEXT PRIMARY KEY,
            title        TEXT,
            price        INTEGER,
            kilometres   INTEGER,
            year         INTEGER,
            make         TEXT,
            model        TEXT,
            url          TEXT,
            date_scraped TEXT,
            is_new       INTEGER DEFAULT 1
        )
    """)
    con.commit()
    return con


def save_listings(con, listings: list[dict]) -> list[dict]:
    """Insert new listings; return only the ones we haven't seen before."""
    new_ones = []
    for car in listings:
        try:
            con.execute("""
                INSERT INTO listings
                  (listing_id, title, price, kilometres, year, make, model, url, date_scraped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                car["listing_id"], car["title"], car["price"],
                car["kilometres"], car["year"], car["make"],
                car["model"], car["url"], car["date_scraped"],
            ))
            new_ones.append(car)
        except sqlite3.IntegrityError:
            pass  # already in DB — not new
    con.commit()
    return new_ones


# ── Scraper ───────────────────────────────────────────────────────────────────

def parse_price(text: str) -> int | None:
    """
    Extract a likely listing price from text.
    Prefer explicit "Asking price" or "Reserve met" values; fall back to context scoring.
    """
    if not text:
        return None

    # Fast path: explicit labels
    for label in ["asking price", "reserve met", "buy now"]:
        m = re.search(rf"{label}\s*\$\s?\d[\d,]*", text, re.IGNORECASE)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(0))
            return int(digits) if digits else None

    # Capture $ amounts with some surrounding context
    candidates = []
    for m in re.finditer(r"\$\s?\d[\d,]*", text):
        raw = m.group(0)
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            continue
        price = int(digits)

        # Filter out tiny/garbage values (e.g., "$7" from non price UI)
        if price < 100:
            continue

        # Grab nearby context (lowercased)
        start = max(0, m.start() - 25)
        end = min(len(text), m.end() + 25)
        context = text[start:end].lower()

        score = 0
        # Positive signals for actual listing price
        if "price" in context:
            score += 3
        if "buy now" in context or "buynow" in context:
            score += 3
        if "asking" in context:
            score += 2

        # Negative signals for finance/was/other prices
        if any(k in context for k in ["per week", "weekly", "pw", "/wk", "per month", "monthly", "pm", "/mo"]):
            score -= 5
        if any(k in context for k in ["deposit", "down", "finance", "from $", "from", "starting"]):
            score -= 4
        if any(k in context for k in ["was", "rrp", "retail", "save", "valued"]):
            score -= 3

        candidates.append((score, price))

    if not candidates:
        return None

    # Prefer highest score; if tie, prefer the higher price (likely full listing)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][1]


def parse_kms(text: str) -> int | None:
    """Extract km integer from strings like '145,000 km'."""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_year(title: str) -> int | None:
    """Try to pull a 4-digit year (1980-2026) from the listing title."""
    match = re.search(r"\b(19[89]\d|20[0-2]\d|2026)\b", title)
    return int(match.group()) if match else None


def normalize_listing_id(href: str) -> str:
    """Extract stable listing id from URL (ignores query params)."""
    if not href:
        return ""
    # Prefer /listing/<id>
    m = re.search(r"/listing/(\d+)", href)
    if m:
        return m.group(1)
    # Fallback: last path segment without query params
    href_no_q = href.split("?", 1)[0]
    m = re.search(r"/(\d+)$", href_no_q)
    return m.group(1) if m else href_no_q


def guess_make(title: str) -> str:
    """Naive make extractor — grabs the first capitalised word."""
    known = [
        "Toyota", "Honda", "Mazda", "Nissan", "Subaru", "Mitsubishi",
        "Ford", "Holden", "Hyundai", "Kia", "Suzuki", "BMW", "Mercedes",
        "Audi", "Volkswagen", "VW", "Isuzu", "Jeep", "Land Rover",
    ]
    for make in known:
        if make.lower() in title.lower():
            return make
    return "Unknown"


def guess_model(title: str, make: str) -> str:
    """Guess model as the word after the make in the title."""
    if not title or not make or make == "Unknown":
        return title

    # Normalize for matching but keep original tokens for output
    tokens = re.split(r"\s+", title.strip())
    lower_tokens = [t.lower() for t in tokens]

    # Handle VW alias
    make_variants = {make.lower()}
    if make.lower() == "volkswagen":
        make_variants.add("vw")
    if make.lower() == "vw":
        make_variants.add("volkswagen")

    for i, tok in enumerate(lower_tokens):
        if tok in make_variants:
            if i + 1 < len(tokens):
                # Keep the next token as-is (including hyphens)
                return tokens[i + 1]
            break

    return title


async def scrape_page(page, url: str) -> list[dict]:
    """Load one search-results page and extract all car listings."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    # Give JS a moment to render cards
    await asyncio.sleep(random.uniform(2, 4))

    # TradeMe listing cards — selector may need updating if they redesign the site
    cards = await page.query_selector_all('[data-testid="listing-card"]')

    # Fallback selectors if the above returns nothing
    if not cards:
        cards = await page.query_selector_all(".tm-motors-search-card")
    if not cards:
        cards = await page.query_selector_all("li.o-card")

    listings = []

    def build_listing_from_text(href: str, text: str) -> dict:
        price = parse_price(text)

        kms_match = re.search(r"([\d,]+)\s*km", text, re.IGNORECASE)
        kms = parse_kms(kms_match.group(1)) if kms_match else None

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        title_line = next((ln for ln in lines if parse_year(ln)), None)
        title = title_line or (lines[-1] if lines else "Unknown")

        listing_id = normalize_listing_id(href)

        return {
            "listing_id": listing_id,
            "title":      title,
            "price":      price,
            "kilometres": kms,
            "year":       parse_year(title),
            "make":       guess_make(title),
            "model":      guess_model(title, guess_make(title)),
            "url":        href,
            "date_scraped": datetime.now().isoformat(timespec="seconds"),
        }

    if cards:
        for card in cards:
            try:
                # Title
                title_el = await card.query_selector("h2, h3, [data-testid='listing-title']")
                title = (await title_el.inner_text()).strip() if title_el else "Unknown"

                # Price
                price_el = await card.query_selector("[data-testid='price'], .tm-motors-search-card__price, .price")
                price_text = (await price_el.inner_text()).strip() if price_el else ""
                price = parse_price(price_text)

                # Kilometres — look for a "km" element in the card
                kms_el = await card.query_selector("[data-testid='odometer'], .odometer")
                if not kms_el:
                    # Fallback: search all text in the card for a km pattern
                    card_text = await card.inner_text()
                    kms_match = re.search(r"([\d,]+)\s*km", card_text, re.IGNORECASE)
                    kms = parse_kms(kms_match.group(1)) if kms_match else None
                else:
                    kms = parse_kms(await kms_el.inner_text())

                # URL & listing ID
                link_el = await card.query_selector("a")
                href = await link_el.get_attribute("href") if link_el else ""
                if href and not href.startswith("http"):
                    href = "https://www.trademe.co.nz" + href
                listing_id = normalize_listing_id(href)

                make = guess_make(title)
                listings.append({
                    "listing_id": listing_id,
                    "title":      title,
                    "price":      price,
                    "kilometres": kms,
                    "year":       parse_year(title),
                    "make":       make,
                    "model":      guess_model(title, make),
                    "url":        href,
                    "date_scraped": datetime.now().isoformat(timespec="seconds"),
                })
            except Exception as e:
                print(f"  ⚠️  Skipped a card: {e}")
    else:
        # Newer TradeMe pages sometimes expose listing details via link text/ARIA.
        # Fallback: extract from listing links directly.
        link_els = await page.query_selector_all("a[href*='/a/motors/cars/']")
        for link in link_els:
            try:
                href = await link.get_attribute("href")
                if not href:
                    continue
                if "/listing/" not in href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.trademe.co.nz" + href

                aria = (await link.get_attribute("aria-label")) or ""
                text = (await link.inner_text()).strip()
                if not text:
                    text = aria

                # Pull surrounding text from the closest reasonable container to capture price/kms.
                container_text = await link.evaluate(
                    """
                    el => {
                      const maxDepth = 6;
                      let cur = el;
                      for (let i = 0; i < maxDepth && cur; i++) {
                        const text = cur.innerText || "";
                        if (text && text.length < 1200 && /Asking price|Reserve met|Buy now|\\$/.test(text)) {
                          return text;
                        }
                        cur = cur.parentElement;
                      }
                      const li = el.closest("li");
                      if (li && li.innerText && li.innerText.length < 1200) return li.innerText;
                      const article = el.closest("article");
                      if (article && article.innerText && article.innerText.length < 1200) return article.innerText;
                      return "";
                    }
                    """
                )
                combined_text = "\n".join([t for t in [text, container_text] if t])

                listings.append(build_listing_from_text(href, combined_text))
            except Exception as e:
                print(f"  ⚠️  Skipped a link: {e}")

    return listings


def build_base_url(make: str, region_id: int, min_price: int, max_price: int, max_kms: int, year_min: int) -> str:
    return (
        f"https://www.trademe.co.nz/a/motors/cars/{make}/search"
        f"?user_region={region_id}"
        f"&price_min={min_price}"
        f"&price_max={max_price}"
        f"&odometer_max={max_kms}"
        f"&sort_order=motorslatestlistings"
        f"&year_min={year_min}"
    )


async def scrape_all_pages(base_url: str, max_pages: int = MAX_PAGES) -> list[dict]:
    """Scrape up to max_pages of results."""
    all_listings = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for page_num in range(1, max_pages + 1):
            url = base_url + (f"&page={page_num}" if page_num > 1 else "")
            # print(f"  Scraping page {page_num}: {url}")
            try:
                page_listings = await scrape_page(page, url)
                # print(f"    Found {len(page_listings)} listings")
                all_listings.extend(page_listings)
            except Exception as e:
                print(f"  ❌ Error on page {page_num}: {e}")
                break

            # Polite delay between pages
            await asyncio.sleep(random.uniform(3, 6))

        await browser.close()

    return all_listings


# ── Deal scoring ──────────────────────────────────────────────────────────────

def score_deals(con, new_listings: list[dict]) -> list[dict]:
    """
    Score each new listing against the median price of similar cars
    already in the database (same make, similar year ±3 years).
    Falls back to overall median if not enough comps exist.
    """
    scored = []
    for car in new_listings:
        if not car["price"]:
            continue

        # Comparable listings: same make, same model, year within ±3
        year = car["year"] or 2000
        rows = con.execute("""
            SELECT price FROM listings
            WHERE make = ?
              AND model = ?
              AND year BETWEEN ? AND ?
              AND price IS NOT NULL
              AND listing_id != ?
        """, (car["make"], car["model"], year - 3, year + 3, car["listing_id"])).fetchall()

        prices = [r[0] for r in rows]

        if len(prices) >= 3:
            median = sorted(prices)[len(prices) // 2]
        else:
            # Fall back to overall median in DB
            all_rows = con.execute(
                "SELECT price FROM listings WHERE price IS NOT NULL"
            ).fetchall()
            all_prices = sorted(r[0] for r in all_rows)
            median = all_prices[len(all_prices) // 2] if all_prices else car["price"]

        price_pct = round((car["price"] / median) * 100, 1)
        car["median_comp"] = median
        car["saving_pct"]  = price_pct
        scored.append(car)

    # Sort best deals first (lower % of median is better)
    scored.sort(key=lambda c: c["saving_pct"])
    return scored


# ── Main entry point ──────────────────────────────────────────────────────────

async def run():
    base_url = build_base_url(
        make=MAKE,
        region_id=REGION_ID,
        min_price=MIN_PRICE,
        max_price=MAX_PRICE,
        max_kms=MAX_KMS,
        year_min=YEAR_MIN,
    )

    print(f"\nTradeMe Car Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(
        f"Filters: make {MAKE}  |  year >= {YEAR_MIN}  |  "
        f"min ${MIN_PRICE:,}  |  max ${MAX_PRICE:,}  |  "
        f"max {MAX_KMS:,} km  |  region id {REGION_ID}\n"
    )

    con = init_db()

    print("Scraping TradeMe...")
    all_listings = await scrape_all_pages(base_url=base_url, max_pages=MAX_PAGES)
    print(f"\nScraped {len(all_listings)} listings total")

    new_listings = save_listings(con, all_listings)
    print(f"{len(new_listings)} are new (not seen before)")

    if not new_listings:
        print("Nothing new today.")
        con.close()
        return

    scored = score_deals(con, new_listings)
    con.close()
    return scored


if __name__ == "__main__":
    results = asyncio.run(run())
    if results:
        print(f"\nTop 5 deals:")
        for car in results[:5]:
            print(
                f"  {car['saving_pct']:.0f}%  ${car['price']:,}  "
                f"(median ${car['median_comp']:,})  — {car['title'][:60]}"
            )
            if car.get("url"):
                print(f"     {car['url']}")
        print("\nRun mailer.py to send the email report.")
