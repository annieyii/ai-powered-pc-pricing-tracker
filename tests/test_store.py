"""Storage and validation tests.

Fixtures never touch the real CSVs: every sample file is written under tmp_path.
"""
import sqlite3

import pandas as pd
import pytest

from tracker.store import (
    Dataset,
    build,
    connect,
    ingest_prices,
    ingest_products,
    init_db,
    read_prices,
    read_products,
)

PRODUCTS_CSV = "data/structured/products.csv"
HEADER = ("sku,captured_at_local,price,regular_price,savings,"
          "availability,seller,stock_hint,note\n")
LENOVO, HP = "6672159", "6668002"


@pytest.fixture()
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def seeded(conn):
    ingest_products(conn, PRODUCTS_CSV)
    return conn


def csv_at(tmp_path, body):
    p = tmp_path / "prices.csv"
    p.write_text(HEADER + body, encoding="utf-8")
    return p


# --- schema enforces the invariants -------------------------------------

def test_unknown_sku_is_refused_by_the_foreign_key(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price_snapshots (sku, captured_at, price, availability, seller)"
            " VALUES ('9999999','2026-09-12T15:07',1.0,'Unavailable','Best Buy')")


def test_role_outside_the_enum_is_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO products (sku, role, brand, model_name, cpu, ram_gb,"
            " storage_gb, screen_inch, form_factor, list_price, source_url)"
            " VALUES ('1','maybe','X','Y','Z',16,512,14.0,'2-in-1',1.0,'u')")


def test_ingest_products_loads_the_real_master(conn):
    assert ingest_products(conn, PRODUCTS_CSV) == 4
    frame = read_products(conn)
    assert set(frame["role"]) == {"strict", "reference"}
    assert frame["sku"].dtype == object
    assert (frame["role"] == "strict").sum() == 2


# --- ingest accepts what it should --------------------------------------

def test_a_valid_row_is_stored(seeded, tmp_path):
    csv = csv_at(tmp_path, f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n")
    data = ingest_prices(seeded, csv)
    assert data.accepted == 1 and data.total == 1 and data.rejected == []


def test_blank_price_is_allowed_when_the_listing_is_not_purchasable(seeded, tmp_path):
    csv = csv_at(tmp_path, f"{LENOVO},2026-09-12T15:07,,,,Unavailable,Best Buy,,\n")
    data = ingest_prices(seeded, csv)
    assert data.accepted == 1
    assert pd.isna(read_prices(seeded).loc[0, "price"])


def test_savings_consistent_with_regular_minus_price_is_accepted(seeded, tmp_path):
    csv = csv_at(tmp_path, f"{LENOVO},2026-09-12T15:07,999.99,1299.99,300,Add to cart,Best Buy,,\n")
    assert ingest_prices(seeded, csv).accepted == 1


# --- ingest refuses what it should --------------------------------------

@pytest.mark.parametrize("body,fragment", [
    (f"9999999,2026-09-12T15:07,10.00,,,Add to cart,Best Buy,,\n", "9999999"),
    (f"{LENOVO},not-a-date,1299.99,,,Add to cart,Best Buy,,\n", "timestamp"),
    (f"{LENOVO},2026-09-12,1299.99,,,Add to cart,Best Buy,,\n", "timestamp"),
    (f"{LENOVO},09/12/2026 15:07,1299.99,,,Add to cart,Best Buy,,\n", "timestamp"),
    (f"{LENOVO},2026-09-12T15:07,,,,Add to cart,Best Buy,,\n", LENOVO),
    (f"{LENOVO},2026-09-12T15:07,99999,,,Add to cart,Best Buy,,\n", LENOVO),
    (f"{LENOVO},2026-09-12T15:07,1299.99,,,In stock,Best Buy,,\n", LENOVO),
    (f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,PC Heaven,,\n", LENOVO),
    (f"{LENOVO},2026-09-12T15:07,999.99,1299.99,250,Add to cart,Best Buy,,\n", LENOVO),
])
def test_bad_rows_are_refused_with_a_reason(seeded, tmp_path, body, fragment):
    data = ingest_prices(seeded, csv_at(tmp_path, body))
    assert data.accepted == 0 and data.total == 1
    assert len(data.rejected) == 1
    assert fragment in (data.rejected[0].sku + " " + data.rejected[0].reason)
    assert data.rejected[0].line == 2


def test_a_repeated_key_within_one_file_is_refused_not_silently_dropped(seeded, tmp_path):
    csv = csv_at(tmp_path,
                 f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n"
                 f"{LENOVO},2026-09-12T15:07,1199.99,,,Add to cart,Best Buy,,\n")
    data = ingest_prices(seeded, csv)
    assert data.accepted == 1
    assert len(data.rejected) == 1
    assert read_prices(seeded).loc[0, "price"] == 1299.99


def test_blank_fields_never_become_the_string_nan(seeded, tmp_path):
    csv = csv_at(tmp_path, f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n")
    ingest_prices(seeded, csv)
    row = read_prices(seeded).loc[0]
    assert row["stock_hint"] is None and row["note"] is None
    assert pd.isna(row["regular_price"])


# --- reads --------------------------------------------------------------

def test_read_prices_is_typed_and_time_ordered(seeded, tmp_path):
    csv = csv_at(tmp_path,
                 f"{LENOVO},2026-09-12T21:00,1199.99,,,Add to cart,Best Buy,,\n"
                 f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n")
    ingest_prices(seeded, csv)
    frame = read_prices(seeded)
    assert list(frame["price"]) == [1299.99, 1199.99]
    assert frame["captured_at"].dtype.kind == "M"


def test_read_prices_on_an_empty_table_still_has_columns(seeded):
    frame = read_prices(seeded)
    assert frame.empty
    for column in ("sku", "captured_at", "price", "availability"):
        assert column in frame.columns


# --- build rebuilds from the CSVs every time ----------------------------

def test_build_reflects_a_corrected_price_rather_than_the_first_one(tmp_path):
    csv = csv_at(tmp_path, f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n")
    first = build(PRODUCTS_CSV, csv)
    assert first.prices.loc[0, "price"] == 1299.99

    csv.write_text(HEADER + f"{LENOVO},2026-09-12T15:07,1199.99,,,Add to cart,Best Buy,,\n",
                   encoding="utf-8")
    second = build(PRODUCTS_CSV, csv)
    assert second.prices.loc[0, "price"] == 1199.99, "a persisted store would keep 1299.99"


def test_build_drops_a_row_removed_from_the_csv(tmp_path):
    csv = csv_at(tmp_path,
                 f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n"
                 f"{HP},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n")
    assert len(build(PRODUCTS_CSV, csv).prices) == 2

    csv.write_text(HEADER + f"{HP},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n",
                   encoding="utf-8")
    after = build(PRODUCTS_CSV, csv)
    assert len(after.prices) == 1 and after.prices.loc[0, "sku"] == HP


def test_build_reports_accepted_and_total(tmp_path):
    csv = csv_at(tmp_path,
                 f"{LENOVO},2026-09-12T15:07,1299.99,,,Add to cart,Best Buy,,\n"
                 f"9999999,2026-09-12T15:07,1.00,,,Add to cart,Best Buy,,\n")
    data = build(PRODUCTS_CSV, csv)
    assert isinstance(data, Dataset)
    assert (data.accepted, data.total) == (1, 2)
    assert len(data.products) == 4
    assert data.rejected[0].line == 3


def test_build_on_a_header_only_file(tmp_path):
    data = build(PRODUCTS_CSV, csv_at(tmp_path, ""))
    assert data.prices.empty and data.rejected == [] and data.total == 0
