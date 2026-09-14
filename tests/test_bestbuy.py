"""Best Buy API adapter tests.

No key exists and nothing here reaches the network: the opener is injected and
the payload is a recorded shape built from the published attribute list. What
these tests are actually for is the join between the adapter and the store. An
adapter whose rows the schema then refuses is worse than no adapter, because
the failure would only appear once someone had a key and was in a hurry.
"""
import json
from datetime import datetime
from io import BytesIO

import pytest

from tracker.bestbuy import (
    REQUEST_TIMEOUT,
    MissingSetting,
    UnmappedValue,
    append_rows,
    fetch,
    product_url,
    read_settings,
    row_from,
)
from tracker.store import ingest_prices, ingest_products, connect, init_db

PRODUCTS_CSV = "data/structured/products.csv"
HEADER = ("sku,captured_at_local,price,regular_price,savings,"
          "availability,seller,stock_hint,note\n")

AT = datetime(2026, 9, 14, 21, 5)

ON_SALE = {
    "sku": 6679150,
    "salePrice": 999.99,
    "regularPrice": 1299.99,
    "onSale": True,
    "dollarSavings": 300.00,
    "orderable": "Available",
    "onlineAvailability": True,
    "inStorePickup": True,
    "priceUpdateDate": "2026-09-10T14:02:00",
}

AT_LIST = {**ON_SALE, "sku": 6672159, "salePrice": 1299.99,
           "regularPrice": 1299.99, "onSale": False, "dollarSavings": 0.0}


def test_the_key_travels_as_a_query_parameter_not_a_header():
    """There is no token exchange and no connection id: the SKU in the path
    and the key in the query string are the whole of the handshake."""
    url = product_url("6672159", "KEY123")
    assert url.startswith("https://api.bestbuy.com/v1/products/6672159.json?")
    assert "apiKey=KEY123" in url


def test_a_store_id_switches_to_the_store_availability_call():
    """Product attributes say a SKU is sold in stores, not that this store has
    it, and every capture in this project is store-fixed."""
    url = product_url("6672159", "KEY123", store_id="1234")
    assert url.startswith("https://api.bestbuy.com/v1/products/6672159/stores.json?")
    assert "storeId=1234" in url


def test_a_missing_key_names_the_variable():
    with pytest.raises(MissingSetting, match="BESTBUY_API_KEY"):
        read_settings({})


def test_a_blank_key_is_not_a_key():
    with pytest.raises(MissingSetting):
        read_settings({"BESTBUY_API_KEY": "   "})


def test_a_promotion_fills_the_regular_price_and_the_savings():
    row = row_from(ON_SALE, AT)
    assert row["price"] == 999.99
    assert row["regular_price"] == 1299.99
    assert row["savings"] == 300.00


def test_off_promotion_leaves_regular_price_blank():
    """The manual convention is that a filled regular_price always means an
    observed discount, so the API's always-present regularPrice is dropped."""
    row = row_from(AT_LIST, AT)
    assert row["price"] == 1299.99
    assert row["regular_price"] is None
    assert row["savings"] is None


def test_the_capture_time_is_the_callers_not_the_api_price_date():
    """priceUpdateDate says when the price last changed, which is not when it
    was observed. Using it would backdate every row."""
    assert row_from(ON_SALE, AT)["captured_at_local"] == "2026-09-14T21:05"
    assert "2026-09-10" in row_from(ON_SALE, AT)["note"]


def test_an_unknown_orderable_value_is_refused_rather_than_defaulted():
    """The value set is not published. An invented availability would enter the
    system of record and look exactly like an observed one."""
    with pytest.raises(UnmappedValue, match="BackOrdered"):
        row_from({**ON_SALE, "orderable": "BackOrdered"}, AT)


def test_a_sold_out_listing_maps_to_a_sold_out_row():
    assert row_from({**ON_SALE, "orderable": "SoldOut"}, AT)["availability"] == "Sold Out"


def test_fetch_does_not_dial_out_when_given_an_opener():
    """The injected opener is the only reason this suite runs offline."""
    calls = []

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    # Same signature as urllib.request.urlopen, so a caller that forgets the
    # timeout is a failure here rather than a request that waits on the
    # operating system's default.
    def opener(url, timeout=None):
        calls.append((url, timeout))
        return FakeResponse(json.dumps(ON_SALE).encode())

    assert fetch("6679150", "KEY123", opener=opener)["sku"] == 6679150
    assert "apiKey=KEY123" in calls[0][0]
    assert calls[0][1] == REQUEST_TIMEOUT


def test_the_adapters_rows_are_accepted_by_the_real_schema(tmp_path):
    """The join that matters. Every CHECK in the store runs against rows the
    adapter produced, so a mapping the schema would refuse fails here rather
    than on the day someone finally has a key."""
    path = tmp_path / "prices.csv"
    path.write_text(HEADER, encoding="utf-8")
    append_rows([row_from(ON_SALE, AT), row_from(AT_LIST, AT)], path)

    conn = connect(":memory:")
    init_db(conn)
    ingest_products(conn, PRODUCTS_CSV)
    result = ingest_prices(conn, path)
    conn.close()

    assert result.rejected == []
    assert result.accepted == 2


def test_a_sold_out_row_without_a_price_is_still_accepted(tmp_path):
    """`availability <> 'Add to cart' OR price IS NOT NULL` is the constraint
    the adapter is most likely to trip, so it is exercised directly."""
    path = tmp_path / "prices.csv"
    path.write_text(HEADER, encoding="utf-8")
    append_rows([row_from({**ON_SALE, "orderable": "SoldOut",
                           "salePrice": None}, AT)], path)

    conn = connect(":memory:")
    init_db(conn)
    ingest_products(conn, PRODUCTS_CSV)
    result = ingest_prices(conn, path)
    conn.close()

    assert result.rejected == []


def test_rows_follow_the_landing_files_own_header(tmp_path):
    """The landing file gained a column mid-window. Writing the nine columns
    this adapter knows into a ten-column file would shift every later value
    left by one and raise nothing, so the header is read, not assumed."""
    path = tmp_path / "prices.csv"
    path.write_text(HEADER.replace("stock_hint,note", "stock_hint,pickup_eta,note"),
                    encoding="utf-8")
    append_rows([row_from(ON_SALE, AT)], path)

    conn = connect(":memory:")
    init_db(conn)
    ingest_products(conn, PRODUCTS_CSV)
    result = ingest_prices(conn, path)
    conn.close()

    assert result.rejected == []
    line = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert line[-2] == ""            # pickup_eta, which the API does not give
    assert line[-1].startswith("captured via")
