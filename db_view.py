# Run this to view the database

import sqlite3
from pathlib import Path
from textwrap import shorten


DB_PATH = Path(__file__).parent / "trademe_cars.db"


def main(limit: int = 10):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT listing_id, title, price, kilometres, year, make, model, url, date_scraped
        FROM listings
        ORDER BY date_scraped DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    con.close()

    if not rows:
        print("No rows found.")
        return

    for i, r in enumerate(rows, 1):
        listing_id, title, price, kms, year, make, model, url, date_scraped = r
        title = shorten(title or "", width=60, placeholder="…")
        print(
            f"{i}. {title}\n"
            f"   Price: ${price:,} | KMs: {kms:,} | Year: {year} | Make: {make} | Model: {model}\n"
            f"   URL: {url}\n"
            f"   Scraped: {date_scraped}\n"
        )


if __name__ == "__main__":
    import sys

    try:
        limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    except ValueError:
        limit = 10
    main(limit=limit)
