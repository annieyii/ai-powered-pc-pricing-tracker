"""Capture helper tests. No browser is opened and no page is read."""
from datetime import datetime

import pandas as pd
import pytest

import tracker.capture as capture

from tracker.capture import (
    SKIP,
    already_recorded,
    append_rows,
    main,
    open_pages,
    prompt_for,
    session_timestamp,
)

PRODUCTS_CSV = "data/structured/products.csv"
HEADER = ("sku,captured_at_local,price,regular_price,savings,"
          "availability,seller,stock_hint,pickup_eta,note\n")
NOW = datetime(2026, 9, 12, 21, 0)


@pytest.fixture()
def prices(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(HEADER, encoding="utf-8")
    return path


def answers(*values):
    queue = list(values)
    return lambda _prompt: queue.pop(0)


def a_product(sku="6672159", url="https://example.com/x"):
    frame = pd.DataFrame([{"sku": sku, "brand": "Lenovo", "model_name": "Yoga 7a",
                           "source_url": url}])
    return next(frame.itertuples())


def test_one_session_gives_every_product_the_same_timestamp():
    """The strict comparison refuses to pair snapshots from different moments,
    so a session that stamped each product as it was typed could produce rows
    that never compare with each other."""
    assert session_timestamp(NOW) == "2026-09-12T21:00"


def test_pages_are_opened_not_fetched():
    opened = []
    frame = pd.DataFrame([{"sku": "1", "brand": "b", "model_name": "m",
                           "source_url": "https://example.com/a"},
                          {"sku": "2", "brand": "b", "model_name": "m",
                           "source_url": "https://example.com/b"}])
    assert open_pages(frame, opened.append) == 2
    assert opened == ["https://example.com/a", "https://example.com/b"]


def test_prompt_collects_the_seven_observed_fields():
    row = prompt_for(a_product(), answers(
        "1", "1299.99", "", "", "", "Only 1 left", "today", ""))
    assert row["availability"] == "Add to cart"
    assert row["price"] == "1299.99"
    assert row["seller"] == "Best Buy"
    assert row["stock_hint"] == "Only 1 left"
    assert row["pickup_eta"] == "today"


def test_prompt_allows_a_blank_price_when_not_purchasable():
    row = prompt_for(a_product(), answers("2", "", "", "", "", "", "", ""))
    assert row["availability"] == "Unavailable" and row["price"] == ""


def test_prompt_skips_on_a_blank_availability():
    assert prompt_for(a_product(), answers("")) is SKIP


def test_prompt_reasks_on_an_invalid_availability():
    row = prompt_for(a_product(), answers(
        "9", "1", "1299.99", "", "", "", "", "", ""))
    assert row["availability"] == "Add to cart"


def test_append_writes_the_session_timestamp_into_every_row(prices):
    append_rows(prices, "2026-09-12T21:00", [
        {"sku": "6672159", "price": "1299.99", "regular_price": "", "savings": "",
         "availability": "Add to cart", "seller": "Best Buy",
         "stock_hint": "", "pickup_eta": "today", "note": ""}])
    frame = pd.read_csv(prices, dtype={"sku": str})
    assert frame.loc[0, "captured_at_local"] == "2026-09-12T21:00"


def test_append_never_rewrites_an_existing_row(prices):
    prices.write_text(
        HEADER + "6668002,2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,today,\n",
        encoding="utf-8")
    append_rows(prices, "2026-09-12T21:00", [
        {"sku": "6672159", "price": "1199.99", "regular_price": "", "savings": "",
         "availability": "Add to cart", "seller": "Best Buy",
         "stock_hint": "", "pickup_eta": "today", "note": ""}])
    frame = pd.read_csv(prices, dtype={"sku": str})
    assert len(frame) == 2
    assert frame.loc[0, "price"] == 1299.99


def test_already_recorded_detects_a_repeat_of_the_same_session(prices):
    prices.write_text(
        HEADER + "6672159,2026-09-12T21:00,1299.99,,,Add to cart,Best Buy,,today,\n",
        encoding="utf-8")
    assert already_recorded(prices, "6672159", "2026-09-12T21:00")
    assert not already_recorded(prices, "6672159", "2026-09-12T22:00")


def test_main_records_every_product_and_validates(prices, capsys):
    replies = []
    for _ in range(4):
        replies += ["1", "1299.99", "", "", "", "", "today", ""]
    code = main(ask=answers(*replies), opener=lambda _url: None, now=NOW,
                products_csv=PRODUCTS_CSV, prices_csv=prices)
    assert code == 0
    frame = pd.read_csv(prices, dtype={"sku": str})
    assert len(frame) == 4
    assert set(frame["captured_at_local"]) == {"2026-09-12T21:00"}
    assert "4 of 4 rows accepted" in capsys.readouterr().out


def test_main_reports_a_row_the_store_refuses(prices, capsys):
    """A purchasable listing with no price is refused by the schema, and the
    operator is told at once rather than finding out at the dashboard."""
    replies = ["1", "", "", "", "", "", "today", ""]
    for _ in range(3):
        replies += [""]
    code = main(ask=answers(*replies), opener=lambda _url: None, now=NOW,
                products_csv=PRODUCTS_CSV, prices_csv=prices)
    assert code == 2
    assert "refused by the store" in capsys.readouterr().out


def test_main_returns_one_when_everything_is_skipped(prices):
    code = main(ask=answers("", "", "", ""), opener=lambda _url: None, now=NOW,
                products_csv=PRODUCTS_CSV, prices_csv=prices)
    assert code == 1


def test_a_note_never_lands_in_the_pickup_column(prices):
    """The landing file gains columns. Writing a fixed column list into a
    wider file shifts every later value one place left without raising
    anything, and the note would be lost inside pickup_eta."""
    append_rows(prices, "2026-09-12T21:00", [
        {"sku": "6672159", "price": "1299.99", "regular_price": "",
         "savings": "", "availability": "Add to cart", "seller": "Best Buy",
         "stock_hint": "", "pickup_eta": "2026-09-18", "note": "read 21:13"}])
    frame = pd.read_csv(prices, dtype=str, keep_default_na=False)
    assert frame.loc[0, "pickup_eta"] == "2026-09-18"
    assert frame.loc[0, "note"] == "read 21:13"


def test_a_column_the_helper_cannot_fill_is_left_blank(prices, tmp_path):
    """A file wider than this helper knows about still lines up."""
    wide = tmp_path / "wide.csv"
    wide.write_text(HEADER.replace("note", "operator,note"), encoding="utf-8")
    append_rows(wide, "2026-09-12T21:00", [
        {"sku": "6672159", "price": "1299.99", "regular_price": "",
         "savings": "", "availability": "Add to cart", "seller": "Best Buy",
         "stock_hint": "", "pickup_eta": "today", "note": "kept"}])
    frame = pd.read_csv(wide, dtype=str, keep_default_na=False)
    assert frame.loc[0, "operator"] == ""
    assert frame.loc[0, "note"] == "kept"


def test_the_capture_time_is_taipei_whatever_the_machine_is_set_to(monkeypatch):
    """`datetime.now()` with no zone stamps whatever the operator's machine is
    set to, under a banner that says Asia/Taipei. The strict comparison pairs
    snapshots by exact timestamp, so the zone decides what compares with what,
    and the argument is what this asserts because the value alone cannot tell
    a correct stamp from a machine that happens to be in the right zone."""
    asked = []

    class Clock:
        @staticmethod
        def now(tz=None):
            asked.append(tz)
            return datetime(2026, 9, 14, 21, 37, tzinfo=tz)

    monkeypatch.setattr(capture, "datetime", Clock)
    assert capture.session_timestamp() == "2026-09-14T21:37"
    assert asked == [capture.CAPTURE_ZONE]

