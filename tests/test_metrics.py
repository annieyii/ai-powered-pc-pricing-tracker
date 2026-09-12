"""Metric tests. These take DataFrames only: no database, no files, no streamlit."""
import pandas as pd
import pytest

from tracker.metrics import (
    latest_per_sku,
    series_for,
    strict_comparison,
    strict_time_points,
)

LENOVO, HP, DELL = "6672159", "6668002", "6679150"


def prices(rows):
    frame = pd.DataFrame(rows, columns=["sku", "captured_at", "price", "availability"])
    frame["captured_at"] = pd.to_datetime(frame["captured_at"])
    return frame


def products(rows):
    return pd.DataFrame(rows, columns=["sku", "role", "brand", "model_name"])


BOTH_STRICT = products([(LENOVO, "strict", "Lenovo", "Yoga 7a"),
                        (HP, "strict", "HP", "OmniBook X Flip"),
                        (DELL, "reference", "Dell", "14S")])


def test_latest_per_sku_takes_the_most_recent_row_for_each(): 
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (LENOVO, "2026-09-12T21:00", 1199.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart")])
    out = latest_per_sku(frame).set_index("sku")
    assert out.loc[LENOVO, "price"] == 1199.99
    assert out.loc[HP, "price"] == 1299.99


def test_latest_per_sku_on_empty_input():
    assert latest_per_sku(prices([])).empty


# --- the comparison must not pair snapshots from different times --------

def test_comparison_refuses_to_pair_different_timestamps():
    """Regression: taking each SKU's own latest row would compare a 21:00
    Lenovo price against a 15:07 HP price and call the result current."""
    frame = prices([(LENOVO, "2026-09-12T21:00", 1199.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart")])
    out = strict_comparison(frame, BOTH_STRICT)
    assert out["comparable"] is False
    assert "same" in out["reason"]


def test_comparison_uses_the_latest_time_at_which_both_are_priced():
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (LENOVO, "2026-09-12T21:00", 1199.99, "Add to cart"),
                    (HP, "2026-09-12T21:00", 1249.99, "Add to cart")])
    out = strict_comparison(frame, BOTH_STRICT)
    assert out["comparable"] is True
    assert out["captured_at"] == pd.Timestamp("2026-09-12 21:00")
    assert out["cheaper_sku"] == LENOVO
    assert round(out["delta"], 2) == 50.00


def test_comparison_falls_back_to_an_earlier_shared_time():
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1249.99, "Add to cart"),
                    (LENOVO, "2026-09-12T21:00", 1199.99, "Add to cart")])
    out = strict_comparison(frame, BOTH_STRICT)
    assert out["comparable"] is True
    assert out["captured_at"] == pd.Timestamp("2026-09-12 15:07")
    assert out["cheaper_sku"] == HP


def test_comparison_reports_parity_with_direction_absent():
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart")])
    out = strict_comparison(frame, BOTH_STRICT)
    assert out["parity"] is True
    assert out["delta"] == 0.0
    assert out["cheaper_sku"] is None


def test_comparison_ignores_a_reference_product():
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (DELL, "2026-09-12T15:07", 999.99, "Add to cart")])
    out = strict_comparison(frame, BOTH_STRICT)
    assert set(out["skus"]) == {LENOVO, HP}


def test_comparison_skips_a_time_where_one_price_is_missing():
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (LENOVO, "2026-09-12T21:00", 1199.99, "Add to cart"),
                    (HP, "2026-09-12T21:00", float("nan"), "Unavailable")])
    out = strict_comparison(frame, BOTH_STRICT)
    assert out["captured_at"] == pd.Timestamp("2026-09-12 15:07")


def test_comparison_on_no_data():
    out = strict_comparison(prices([]), BOTH_STRICT)
    assert out["comparable"] is False and out["reason"]


# --- time points are counted over strict products only ------------------

def test_time_points_exclude_reference_products():
    frame = prices([(LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (HP, "2026-09-12T15:07", 1299.99, "Add to cart"),
                    (DELL, "2026-09-12T21:00", 999.99, "Add to cart")])
    assert strict_time_points(frame, BOTH_STRICT) == 1


def test_time_points_on_empty_input():
    assert strict_time_points(prices([]), BOTH_STRICT) == 0


def test_series_for_is_time_ordered():
    frame = prices([(LENOVO, "2026-09-12T21:00", 1199.99, "Add to cart"),
                    (LENOVO, "2026-09-12T15:07", 1299.99, "Add to cart")])
    assert list(series_for(frame, LENOVO)["price"]) == [1299.99, 1199.99]


def test_series_for_unknown_sku_is_empty():
    assert series_for(prices([]), "nope").empty
