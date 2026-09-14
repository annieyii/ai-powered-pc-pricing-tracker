"""Open the product pages and record what the person reading them sees.

The program never retrieves a price. It opens the pages in the operator's own
browser, the way a bookmark folder would, and then writes down what they type.
Reading the page is a human act; this only removes the clerical work around it.

One capture session gets ONE timestamp, shared by every product in it. Typing
the time per product would give each SKU a slightly different minute, and the
strict comparison deliberately refuses to pair snapshots taken at different
moments, so a hand-typed session could produce four rows that never compare.
"""
from __future__ import annotations

import csv
import webbrowser
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import pandas as pd

from tracker.store import PRICE_CSV_COLUMNS, TIMESTAMP_FORMAT, build

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS_CSV = ROOT / "data" / "structured" / "products.csv"
PRICES_CSV = ROOT / "data" / "structured" / "prices_manual.csv"

#: The landing file is Asia/Taipei throughout. `datetime.now()` is whatever
#: the operator's machine is set to, and the comparison pairs snapshots by
#: exact timestamp, so the zone decides what compares with what.
CAPTURE_ZONE = ZoneInfo("Asia/Taipei")

AVAILABILITY_CHOICES = {"1": "Add to cart", "2": "Unavailable", "3": "Sold Out"}
SKIP = object()


def session_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(CAPTURE_ZONE)).strftime(TIMESTAMP_FORMAT)


def open_pages(products: pd.DataFrame,
               opener: Callable[[str], Any] = webbrowser.open_new_tab) -> int:
    """Open each product page as a browser tab. Nothing is fetched here."""
    opened = 0
    for product in products.itertuples():
        opener(product.source_url)
        opened += 1
    return opened


def prompt_for(product: Any, ask: Callable[[str], str]) -> dict[str, str] | object:
    """Collect the observed fields for one product, or SKIP it."""
    label = f"{product.brand} {product.model_name} (SKU {product.sku})"
    print(f"\n{label}")
    print(f"  {product.source_url}")

    choice = ask("  availability  1=Add to cart  2=Unavailable  3=Sold Out  "
                 "(blank to skip this product): ").strip()
    if choice == "":
        return SKIP
    while choice not in AVAILABILITY_CHOICES:
        choice = ask("  please answer 1, 2 or 3 (blank to skip): ").strip()
        if choice == "":
            return SKIP
    availability = AVAILABILITY_CHOICES[choice]

    price_hint = "required" if availability == "Add to cart" else "blank if none"
    row = {
        "sku": str(product.sku),
        "price": ask(f"  price ({price_hint}): $").strip(),
        "regular_price": ask("  comp. value / was price (blank if none): $").strip(),
        "savings": ask("  savings (blank if none): $").strip(),
        "availability": availability,
        # Blank stays blank. This defaulted to "Best Buy" on an empty
        # answer, so an operator who never looked at the "Sold by" line
        # still produced a row asserting a first-party listing, which is
        # the one field the whole comparison rests on. The schema refuses
        # a blank seller, which is the right place for that to be caught.
        "seller": ask("  sold by (as shown, blank if not shown): ").strip(),
        "stock_hint": ask("  stock hint (blank if none): ").strip(),
        "pickup_eta": ask("  pickup (today, or a date like 2026-09-18): ").strip(),
        "note": ask("  note (blank if none): ").strip(),
    }
    return row


def already_recorded(prices_csv: Path | str, sku: str, stamp: str) -> bool:
    frame = pd.read_csv(prices_csv, dtype={"sku": str})
    if frame.empty:
        return False
    return bool(((frame["sku"] == sku) &
                 (frame["captured_at_local"] == stamp)).any())


def landing_header(prices_csv: Path | str) -> list[str]:
    """The landing file's own column order.

    Read, never assumed. The file gains columns as a capture starts recording
    more, and writing a fixed list into a wider file shifts every later value
    one column left without raising anything: `note` would land in
    `pickup_eta` and the note would be lost.
    """
    with open(prices_csv, newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle), None) or list(PRICE_CSV_COLUMNS)


def append_rows(prices_csv: Path | str, stamp: str,
                rows: Iterable[dict[str, str]]) -> int:
    """Append to the landing file. Existing rows are never rewritten."""
    header = landing_header(prices_csv)
    written = 0
    with open(prices_csv, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        for row in rows:
            full = {**row, "captured_at_local": stamp}
            writer.writerow({c: full.get(c, "") for c in header})
            written += 1
    return written


def main(ask: Callable[[str], str] = input,
         opener: Callable[[str], Any] = webbrowser.open_new_tab,
         now: datetime | None = None,
         products_csv: Path | str = PRODUCTS_CSV,
         prices_csv: Path | str = PRICES_CSV) -> int:
    products = pd.read_csv(products_csv, dtype={"sku": str})
    stamp = session_timestamp(now)

    print(f"Capture session {stamp} (Asia/Taipei)")
    print(f"Opening {len(products)} product pages. Read each price from the page.")
    open_pages(products, opener)

    rows: list[dict[str, str]] = []
    for product in products.itertuples():
        if already_recorded(prices_csv, str(product.sku), stamp):
            print(f"\n{product.sku}: already recorded at {stamp}, skipping")
            continue
        row = prompt_for(product, ask)
        if row is SKIP:
            print("  skipped")
            continue
        rows.append(row)

    if not rows:
        print("\nNothing recorded.")
        return 1

    append_rows(prices_csv, stamp, rows)
    print(f"\nAppended {len(rows)} row(s) at {stamp}.")

    data = build(products_csv, prices_csv)
    print(f"Validation: {data.accepted} of {data.total} rows accepted.")
    for rejection in data.rejected:
        print(f"  line {rejection.line} (sku {rejection.sku}): {rejection.reason}")
    return 0 if not data.rejected else 2


if __name__ == "__main__":
    raise SystemExit(main())
