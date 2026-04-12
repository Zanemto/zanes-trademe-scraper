"""
Zanes TradeMe Car Scraper
Scrapes newly listed cars from TradeMe matching your filters and saves them to the DB.
Run mailer.py separately to score deals and send the email report.
"""

import asyncio
import json
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "trademe_cars.db"
FILTERS_PATH = BASE_DIR / "scraper_filters.json"


def load_scraper_config() -> dict:
    """Load scraper config (max_pages + filter sets) from scraper_filters.json."""
    if FILTERS_PATH.exists():
        return json.loads(FILTERS_PATH.read_text())
    return {"max_pages": 10, "filter_sets": []}


# ── Database setup ────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
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
            listed_as    TEXT,
            region_id    INTEGER,
            is_new       INTEGER DEFAULT 1
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_listings_make_model_year
        ON listings (make, model, year)
    """)
    # Migrate existing DBs that predate the listed_as column
    existing = {row[1] for row in con.execute("PRAGMA table_info(listings)").fetchall()}
    if "listed_as" not in existing:
        con.execute("ALTER TABLE listings ADD COLUMN listed_as TEXT")
    if "region_id" not in existing:
        con.execute("ALTER TABLE listings ADD COLUMN region_id INTEGER")
    con.commit()
    return con


def save_listings(con, listings: list[dict]) -> list[dict]:
    """Insert new listings; return only the ones we haven't seen before."""
    new_ones = []
    for car in listings:
        try:
            con.execute("""
                INSERT INTO listings
                  (listing_id, title, price, kilometres, year, make, model, url, date_scraped, listed_as, region_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                car["listing_id"], car["title"], car["price"],
                car["kilometres"], car["year"], car["make"],
                car["model"], car["url"], car["date_scraped"],
                car.get("listed_as"), car.get("region_id"),
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


def build_base_url(
    make: str,
    model: str | None,
    region_id: int | None,
    min_price: int | None,
    max_price: int | None,
    max_kms: int | None,
    year_min: int | None,
    classifieds: bool | None = None,
) -> str:
    make_path = f"/{make}" if make else ""
    model_path = f"/{model}" if model else ""
    base = f"https://www.trademe.co.nz/a/motors/cars{make_path}{model_path}/search"

    params = ["auto_category_jump=false"]
    if year_min is not None:
        params.append(f"year_min={year_min}")
    if max_kms is not None:
        params.append(f"odometer_max={max_kms}")
    if max_price is not None:
        params.append(f"price_max={max_price}")
    if min_price is not None:
        params.append(f"price_min={min_price}")
    if region_id is not None:
        params.append(f"user_region={region_id}")
    if classifieds is True:
        params.append("listed_as=classifieds")
    elif classifieds is False:
        params.append("listed_as=auctions")

    return base + "?" + "&".join(params)


async def scrape_all_pages(base_url: str, max_pages: int = 10) -> list[dict]:
    """Scrape up to max_pages of results."""
    all_listings = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
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
                if not page_listings:
                    # Stop early if this page has no results
                    break
            except Exception as e:
                print(f"  ❌ Error on page {page_num}: {e}")
                break

            # Polite delay between pages
            await asyncio.sleep(random.uniform(3, 6))

        await browser.close()

    return all_listings


# ── Main entry point ──────────────────────────────────────────────────────────

async def run(selected_names: set | None = None):
    print(f"\nTradeMe Car Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    def format_filter_set(fset: dict) -> str:
        parts = [
            f"make {fset.get('make')}",
            f"model {fset.get('model')}" if fset.get("model") else None,
            f"year >= {fset.get('year_min')}" if fset.get("year_min") is not None else None,
            f"min ${fset.get('min_price'):,}" if fset.get("min_price") is not None else None,
            f"max ${fset.get('max_price'):,}" if fset.get("max_price") is not None else None,
            f"max {fset.get('max_kms'):,} km" if fset.get("max_kms") is not None else None,
            f"region id {fset.get('region_id')}" if fset.get("region_id") is not None else None,
        ]
        label = fset.get("name") or fset.get("make") or "Filter set"
        return f"{label}: " + "  |  ".join(p for p in parts if p)

    config = load_scraper_config()
    filter_sets = config.get("filter_sets", [])
    max_pages = config.get("max_pages", 10)

    if selected_names is not None:
        filter_sets = [f for f in filter_sets if f.get("name") in selected_names]

    if not filter_sets:
        print("No filter sets configured. Add some in the UI or scraper_filters.json.")
        return

    for fset in filter_sets:
        print(format_filter_set(fset))
    print("")

    con = init_db()

    print("Scraping TradeMe...")
    all_listings = []
    for fset in filter_sets:
        classifieds = fset.get("classifieds")
        base_url = build_base_url(
            make=fset.get("make"),
            model=fset.get("model"),
            region_id=fset.get("region_id"),
            min_price=fset.get("min_price"),
            max_price=fset.get("max_price"),
            max_kms=fset.get("max_kms"),
            year_min=fset.get("year_min"),
            classifieds=classifieds,
        )
        listed_as = "classifieds" if classifieds is True else ("auctions" if classifieds is False else None)
        scraped = await scrape_all_pages(base_url=base_url, max_pages=max_pages)
        for listing in scraped:
            listing["listed_as"] = listed_as
            listing["region_id"] = fset.get("region_id")
        all_listings.extend(scraped)
    print(f"\nScraped {len(all_listings)} listings total")

    new_listings = save_listings(con, all_listings)
    print(f"{len(new_listings)} are new (not seen before)")
    con.close()

    if not new_listings:
        print("Nothing new today.")
    else:
        print("\nRun mailer.py to send the email report.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--filters", type=str, default=None,
                        help="Comma-separated filter set names to run (default: all)")
    args = parser.parse_args()
    selected = set(args.filters.split(",")) if args.filters else None
    asyncio.run(run(selected_names=selected))
