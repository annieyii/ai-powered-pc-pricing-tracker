"""Best Buy Products API adapter: the seam manual capture would be replaced by.

Nothing in the app imports this module and no test reaches the network. It
exists so that swapping manual capture for an authorised feed is a matter of
credentials rather than design, and so the shape of that swap can be read
rather than promised.

**It is not integrated and not live-tested.** No key was available, so the
mapping below was written from the published attribute documentation and is
exercised only against a hand-built payload in `tests/test_bestbuy.py`. That
payload is the documented shape, not a saved response from the real service.
`main` fetches the product endpoint only; the store-availability form that a
store-fixed capture needs is built by `product_url` but called by nothing. Every
place where the documentation does not settle a question is marked `TODO:`
with the value that has to be confirmed on the first real call.

Why the output is a CSV row rather than a database write: `prices_manual.csv`
is the system of record and its git diff is the audit trail. An automated
capture that wrote past it would be a second, invisible source of truth. So
this appends the same nine columns a human would have typed, and everything
downstream stays unchanged.

Usage, once a key exists:

    BESTBUY_API_KEY=... python -m tracker.bestbuy --dry-run
    BESTBUY_API_KEY=... python -m tracker.bestbuy >> /dev/null
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from tracker.store import PRICE_CSV_COLUMNS, TIMESTAMP_FORMAT

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS_CSV = ROOT / "data" / "structured" / "products.csv"
PRICES_CSV = ROOT / "data" / "structured" / "prices_manual.csv"

CAPTURE_ZONE = ZoneInfo("Asia/Taipei")

# One product by SKU. The SKU is the join key throughout: it is what the CSVs
# key on, what the URL path takes, and what the store-availability call below
# takes. Nothing else has to be matched up.
PRODUCT_URL = "https://api.bestbuy.com/v1/products/{sku}.json"

# Store-level availability is a separate call, because the product attributes
# say whether a SKU is sold in stores at all, not whether this store has it.
# The manual capture is store-fixed, so a faithful replacement needs both.
STORE_URL = "https://api.bestbuy.com/v1/products/{sku}/stores.json"

# Only the attributes that map to a column. Asking for `all` would return
# around seventy fields, none of which this pipeline records.
SHOW = ("sku,salePrice,regularPrice,onSale,dollarSavings,orderable,"
        "onlineAvailability,inStorePickup,priceUpdateDate")

# TODO: unverified. The documentation names `orderable` as "product ordering
# status" without publishing its value set, and these three strings are a
# reading of it, not a quote. Confirm on the first live call. An unlisted value
# raises rather than falling through to a default, because guessing here would
# put an invented availability into the system of record.
ORDERABLE = {
    "Available": "Add to cart",
    "SoldOut": "Sold Out",
    "Unavailable": "Unavailable",
}

REQUIRED_ENV = ("BESTBUY_API_KEY",)

#: Seconds to wait on one request. Without it, urlopen waits on the operating
#: system's default, which on a hung connection is minutes.
REQUEST_TIMEOUT = 30.0


class MissingSetting(Exception):
    """A required environment variable was absent or blank."""


class UnmappedValue(Exception):
    """The API returned a value this adapter will not guess at."""


def read_settings(env: Mapping[str, str]) -> str:
    """The API key, refusing to guess at it.

    Mirrors `extract.read_settings`: no default, and the message names the
    variable rather than failing later as an HTTP 403 far from its cause.
    """
    for name in REQUIRED_ENV:
        if not (env.get(name) or "").strip():
            raise MissingSetting(
                f"{name} is not set. Request a key at "
                f"https://developer.bestbuy.com and see .env.example. "
                f"No key is assumed and no request is made without one.")
    return env["BESTBUY_API_KEY"].strip()


def product_url(sku: str, api_key: str, store_id: str | None = None) -> str:
    """The request URL for one SKU.

    The key travels as the `apiKey` query parameter; there is no header and no
    token exchange. `store_id` selects the store-availability form.
    """
    template = STORE_URL if store_id else PRODUCT_URL
    query = {"apiKey": api_key, "format": "json"}
    if store_id:
        query["storeId"] = store_id
    else:
        query["show"] = SHOW
    return f"{template.format(sku=sku)}?{urllib.parse.urlencode(query)}"


def _money(value: Any) -> float | None:
    if value in (None, "") or (isinstance(value, float) and pd.isna(value)):
        return None
    return round(float(value), 2)


def row_from(payload: Mapping[str, Any], captured_at: datetime) -> dict[str, Any]:
    """One payload, mapped to the nine columns a human would have typed.

    Pure, so the mapping is testable without a key. The caller supplies the
    capture time: the API's own `priceUpdateDate` says when the price last
    changed, which is a different fact from when this observation was taken,
    and conflating them would backdate every row.
    """
    orderable = payload.get("orderable")
    if orderable not in ORDERABLE:
        raise UnmappedValue(
            f"orderable={orderable!r} is not one of {sorted(ORDERABLE)}. "
            f"The row is refused rather than given an invented availability.")

    on_sale = bool(payload.get("onSale"))
    note = [f"captured via Best Buy Products API; "
            f"priceUpdateDate={payload.get('priceUpdateDate')}"]
    if not payload.get("inStorePickup"):
        note.append("inStorePickup false")

    return {
        "sku": str(payload["sku"]),
        "captured_at_local": captured_at.strftime(TIMESTAMP_FORMAT),
        "price": _money(payload.get("salePrice")),
        # The manual convention leaves these blank off-promotion, so that a
        # filled regular_price always means an observed discount.
        "regular_price": _money(payload.get("regularPrice")) if on_sale else None,
        "savings": _money(payload.get("dollarSavings")) if on_sale else None,
        "availability": ORDERABLE[orderable],
        # TODO: the Products API exposes no verified first-party/marketplace
        # flag, and the store's CHECK constraint admits 'Best Buy' only. The
        # manual process read "Sold by Best Buy" off the page. Confirm which
        # attribute carries this before trusting an automated row.
        "seller": "Best Buy",
        "stock_hint": "",
        "note": "; ".join(note),
    }


def fetch(sku: str, api_key: str,
          opener: Callable[[str], Any] = urllib.request.urlopen) -> dict[str, Any]:
    """The payload for one SKU, product endpoint only.

    `opener` is injected so tests never dial out. The store-availability form is
    deliberately not reached from here: its response nests stores under the
    product and `row_from` would not read it.
    """
    with opener(product_url(sku, api_key), timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def append_rows(rows: list[dict[str, Any]], path: Path = PRICES_CSV) -> int:
    """Append to the landing file in the column order its own header states.

    The header is read rather than assumed. The landing file gains columns
    (pickup_eta was added mid-window), and writing the nine this module knows
    into a ten-column file would shift every later value one column left
    without raising anything. A column this adapter cannot fill is left blank.
    """
    with open(path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), None) or PRICE_CSV_COLUMNS
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        for row in rows:
            writer.writerow({c: ("" if row.get(c) is None else row[c])
                             for c in header})
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rows instead of appending them")
    args = parser.parse_args(argv)

    try:
        api_key = read_settings(os.environ)
    except MissingSetting as exc:
        print(str(exc), file=sys.stderr)
        return 2

    captured_at = datetime.now(CAPTURE_ZONE)
    skus = pd.read_csv(PRODUCTS_CSV, dtype={"sku": str})["sku"].tolist()

    rows, refused = [], 0
    for sku in skus:
        try:
            rows.append(row_from(fetch(sku, api_key), captured_at))
        # A refusal is reported and the remaining SKUs still run. Nothing is
        # repaired: the missing row simply stays missing, as in the manual path.
        # Broad on purpose: a DNS failure, a timeout, a truncated body and a
        # payload that is not JSON are all one SKU that could not be read, and
        # letting one out abandons every SKU after it. UnmappedValue is this
        # adapter refusing to guess, which is a different thing.
        except (UnmappedValue, urllib.error.URLError, OSError, ValueError,
                KeyError) as exc:
            print(f"{sku}: refused, {exc}", file=sys.stderr)
            refused += 1

    if args.dry_run:
        for row in rows:
            print(row)
    else:
        append_rows(rows)

    print(f"{len(rows)} rows, {refused} refused", file=sys.stderr)
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
