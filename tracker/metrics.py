"""Pure computation over the price tables.

Nothing here opens a file, touches a database or imports streamlit, so every
function is testable from a DataFrame alone.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

STRICT = "strict"


def _strict_skus(products: pd.DataFrame) -> set[str]:
    return set(products.loc[products["role"] == STRICT, "sku"])


def latest_per_sku(prices: pd.DataFrame) -> pd.DataFrame:
    """The most recent row for each SKU.

    Suitable for a table that shows its own capture time per row. Not suitable
    for comparing two SKUs against each other: see strict_comparison.
    """
    if prices.empty:
        return prices.copy()
    return (prices.sort_values("captured_at")
                  .groupby("sku", as_index=False)
                  .tail(1)
                  .reset_index(drop=True))


def strict_comparison(prices: pd.DataFrame, products: pd.DataFrame) -> dict[str, Any]:
    """Compare the strict group at the latest time they were all priced.

    Two prices are only comparable if they were observed at the same moment.
    Pairing each SKU's own latest row would present a 21:00 price and a 15:07
    price as though they were current together, so this walks back through the
    capture times and uses the most recent one where every strict SKU carries a
    usable price.

    Returns ``cheaper_sku`` and a non-negative ``delta``. Parity is a real
    observation, so ``delta`` of zero is reported with no direction rather than
    treated as missing.
    """
    skus = sorted(_strict_skus(products))
    nothing = {"comparable": False, "captured_at": None, "skus": skus,
               "rows": [], "delta": None, "cheaper_sku": None,
               "dearer_sku": None, "parity": None}

    if len(skus) < 2:
        # Under a filter this is the ordinary case, not a missing-data case,
        # and the two read very differently to someone looking at the page.
        return {**nothing,
                "reason": f"the selection holds {len(skus)} of the strict "
                          f"products, and a pair needs two"}
    if prices.empty:
        return {**nothing, "reason": "no snapshots recorded yet"}

    priced = prices[prices["sku"].isin(skus) & prices["price"].notna()]
    for moment in sorted(priced["captured_at"].unique(), reverse=True):
        at_moment = priced[priced["captured_at"] == moment]
        if set(at_moment["sku"]) != set(skus):
            continue

        ordered = at_moment.sort_values("price")
        low, high = ordered.iloc[0], ordered.iloc[-1]
        delta = float(high["price"] - low["price"])
        parity = delta == 0.0
        return {
            "comparable": True,
            "captured_at": pd.Timestamp(moment),
            "skus": skus,
            "rows": ordered[["sku", "price"]].to_dict("records"),
            "delta": delta,
            "cheaper_sku": None if parity else str(low["sku"]),
            "dearer_sku": None if parity else str(high["sku"]),
            "parity": parity,
            "reason": None,
        }

    return {**nothing,
            "reason": "the strict products were never all priced at the same "
                      "capture time"}


def strict_time_points(prices: pd.DataFrame, products: pd.DataFrame) -> int:
    """Distinct capture times covering strict products only.

    Counting every product would claim time points the strict chart does not
    actually plot.
    """
    if prices.empty:
        return 0
    strict = prices[prices["sku"].isin(_strict_skus(products))]
    return int(strict["captured_at"].nunique())


def series_for(prices: pd.DataFrame, sku: str) -> pd.DataFrame:
    """Time-ordered price series for one SKU."""
    if prices.empty:
        return prices.copy()
    rows = prices[prices["sku"] == sku].sort_values("captured_at")
    return rows[["captured_at", "price", "availability"]].reset_index(drop=True)
