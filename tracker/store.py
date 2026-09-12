"""Read the CSVs into a derived SQLite store and query it back.

The CSVs are the system of record. The database is rebuilt in memory on every
load and never persisted: an insert-only store that outlived a load would keep
a price the CSV had since corrected, and keep a row the CSV had since deleted.

What the database is for is enforcement. The invariants live in the DDL, so a
bad row is refused by the schema rather than by a chain of Python checks, and
the refusal carries the database's own explanation. Only the timestamp format
is parsed in Python, because SQLite has no date type.

Nothing is ever repaired. A row that fails is reported with its line number.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M"

SCHEMA = """
CREATE TABLE products (
    sku                TEXT PRIMARY KEY,
    role               TEXT NOT NULL CHECK (role IN ('strict', 'reference')),
    brand              TEXT NOT NULL,
    model_name         TEXT NOT NULL,
    model_number       TEXT,
    cpu                TEXT NOT NULL,
    ram_gb             INTEGER NOT NULL,
    storage_gb         INTEGER NOT NULL,
    screen_inch        REAL NOT NULL,
    display_type       TEXT,
    brightness_nits    INTEGER,
    form_factor        TEXT NOT NULL,
    operating_system   TEXT,
    device_type        TEXT,
    graphics           TEXT,
    touch_screen       TEXT,
    screen_resolution  TEXT,
    list_price         REAL NOT NULL,
    source_url         TEXT NOT NULL
);

CREATE TABLE price_snapshots (
    sku            TEXT NOT NULL REFERENCES products(sku),
    captured_at    TEXT NOT NULL,
    price          REAL,
    regular_price  REAL,
    savings        REAL,
    availability   TEXT NOT NULL
                     CHECK (availability IN ('Add to cart', 'Unavailable', 'Sold Out')),
    seller         TEXT NOT NULL CHECK (seller = 'Best Buy'),
    stock_hint     TEXT,
    note           TEXT,

    PRIMARY KEY (sku, captured_at),

    CHECK (captured_at GLOB
           '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]'),
    CHECK (price IS NULL OR (price > 0 AND price < 10000)),
    CHECK (regular_price IS NULL OR (regular_price > 0 AND regular_price < 10000)),
    CHECK (savings IS NULL OR savings >= 0),
    -- A listing you can add to the cart must carry a price. Learned the hard
    -- way: a delisted SKU returned a stale price with no purchasable offer.
    CHECK (availability <> 'Add to cart' OR price IS NOT NULL),
    -- Savings must reconcile with the two prices when all three are present.
    CHECK (savings IS NULL OR regular_price IS NULL OR price IS NULL
           OR abs(savings - (regular_price - price)) < 0.02)
);
"""

PRODUCT_FIELDS = [
    "sku", "role", "brand", "model_name", "model_number", "cpu",
    "ram_gb", "storage_gb", "screen_inch", "display_type", "brightness_nits",
    "form_factor", "operating_system", "device_type", "graphics",
    "touch_screen", "screen_resolution", "list_price", "source_url",
]

PRICE_FIELDS = [
    "sku", "captured_at", "price", "regular_price", "savings",
    "availability", "seller", "stock_hint", "note",
]

PRICE_CSV_COLUMNS = [
    "sku", "captured_at_local", "price", "regular_price", "savings",
    "availability", "seller", "stock_hint", "note",
]

MONEY_COLUMNS = ("price", "regular_price", "savings")


@dataclass(frozen=True)
class Rejection:
    """One row the store would not accept, kept so the UI can show it."""

    line: int
    sku: str
    raw: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class IngestResult:
    accepted: int
    total: int
    rejected: list[Rejection]


@dataclass(frozen=True)
class Dataset:
    products: pd.DataFrame
    prices: pd.DataFrame
    accepted: int
    total: int
    rejected: list[Rejection]


def connect(db_path: Path | str = ":memory:") -> sqlite3.Connection:
    """Open a connection with foreign keys on.

    SQLite disables foreign keys per connection, so without this pragma the
    REFERENCES clause is decoration.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _cell(value: Any) -> Any:
    """Blank cells become NULL, never the string 'nan'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return None if text == "" or text.lower() == "nan" else value


def ingest_products(conn: sqlite3.Connection, csv_path: Path | str) -> int:
    frame = pd.read_csv(csv_path, dtype={"sku": str, "model_number": str})
    missing = [c for c in PRODUCT_FIELDS if c not in frame.columns]
    if missing:
        raise ValueError(f"products file missing columns: {missing}")

    statement = (f"INSERT INTO products ({', '.join(PRODUCT_FIELDS)}) "
                 f"VALUES ({', '.join('?' for _ in PRODUCT_FIELDS)})")
    count = 0
    for record in frame[PRODUCT_FIELDS].to_dict("records"):
        conn.execute(statement, [_cell(record[f]) for f in PRODUCT_FIELDS])
        count += 1
    conn.commit()
    return count


def ingest_prices(conn: sqlite3.Connection, csv_path: Path | str) -> IngestResult:
    frame = pd.read_csv(csv_path, dtype={"sku": str})
    missing = [c for c in PRICE_CSV_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"prices file missing columns: {missing}")

    statement = (f"INSERT INTO price_snapshots ({', '.join(PRICE_FIELDS)}) "
                 f"VALUES ({', '.join('?' for _ in PRICE_FIELDS)})")

    rejected: list[Rejection] = []
    accepted = 0
    records = frame.to_dict("records")

    for line, record in enumerate(records, start=2):
        sku = str(_cell(record["sku"]) or "").strip()
        raw = {k: _cell(v) for k, v in record.items()}

        stamp = pd.to_datetime(record["captured_at_local"],
                               format=TIMESTAMP_FORMAT, errors="coerce")
        if pd.isna(stamp):
            rejected.append(Rejection(
                line, sku, raw,
                f"timestamp {record['captured_at_local']!r} is not "
                f"{TIMESTAMP_FORMAT}"))
            continue

        values = [sku, stamp.strftime(TIMESTAMP_FORMAT)]
        for column in MONEY_COLUMNS:
            amount = pd.to_numeric(_cell(record[column]), errors="coerce")
            values.append(None if pd.isna(amount) else float(amount))
        values += [
            _cell(record["availability"]),
            _cell(record["seller"]),
            _cell(record["stock_hint"]),
            _cell(record["note"]),
        ]

        try:
            conn.execute(statement, values)
            accepted += 1
        except sqlite3.IntegrityError as exc:
            rejected.append(Rejection(line, sku, raw, f"refused by the store: {exc}"))

    conn.commit()
    return IngestResult(accepted=accepted, total=len(records), rejected=rejected)


def read_products(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM products ORDER BY role DESC, brand", conn, dtype={"sku": str})


def read_prices(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        f"SELECT {', '.join(PRICE_FIELDS)} FROM price_snapshots "
        f"ORDER BY captured_at, sku", conn, dtype={"sku": str})
    frame["captured_at"] = pd.to_datetime(frame["captured_at"],
                                          format=TIMESTAMP_FORMAT)
    for column in MONEY_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def build(products_csv: Path | str, prices_csv: Path | str) -> Dataset:
    """Rebuild the whole store from the CSVs and return what it holds."""
    conn = connect(":memory:")
    try:
        init_db(conn)
        ingest_products(conn, products_csv)
        result = ingest_prices(conn, prices_csv)
        return Dataset(products=read_products(conn), prices=read_prices(conn),
                       accepted=result.accepted, total=result.total,
                       rejected=result.rejected)
    finally:
        conn.close()
