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

import re
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

-- `strict` is the claim the comparison rests on, so the database checks it.
-- A CHECK cannot see other rows; hence triggers. The seven columns are the
-- equivalence rule. `IS NOT`, so a null is a disagreement and not a pass.
CREATE TRIGGER strict_products_must_satisfy_the_equivalence_rule
AFTER INSERT ON products
WHEN NEW.role = 'strict'
BEGIN
    SELECT RAISE(ABORT, 'strict products must match on processor model, memory, storage, screen size, operating system, device type and form factor')
    WHERE EXISTS (SELECT 1 FROM products
                  WHERE role = 'strict' AND sku <> NEW.sku
                    AND (cpu              IS NOT NEW.cpu
                      OR ram_gb           IS NOT NEW.ram_gb
                      OR storage_gb       IS NOT NEW.storage_gb
                      OR screen_inch      IS NOT NEW.screen_inch
                      OR operating_system IS NOT NEW.operating_system
                      OR device_type      IS NOT NEW.device_type
                      OR form_factor      IS NOT NEW.form_factor));
END;

-- A third member turns the pair's delta into a min-max range across products
-- never claimed equivalent to each other.
CREATE TRIGGER the_strict_group_is_a_pair
AFTER INSERT ON products
WHEN NEW.role = 'strict'
BEGIN
    SELECT RAISE(ABORT, 'the strict group is a pair; a third strict product cannot be compared as one')
    WHERE (SELECT count(*) FROM products WHERE role = 'strict') > 2;
END;
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

#: A product column name this module will put into SQL. Identifiers cannot be
#: parameterised, so the only safe way to accept one from a file is to refuse
#: everything that is not a plain name.
SAFE_COLUMN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


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


def extra_product_columns(frame: pd.DataFrame) -> list[str]:
    """Columns the master carries that the schema does not name.

    A product attribute the project has not met before is still an attribute:
    dropping it here would mean a column added to the CSV, or returned by a
    richer feed, never reaching the page. They are carried as TEXT and without
    constraints, because nothing is known about them yet, and a name that is
    not a plain identifier is refused rather than interpolated into SQL.
    """
    unknown = [c for c in frame.columns if c not in PRODUCT_FIELDS]
    bad = [c for c in unknown if not SAFE_COLUMN.match(str(c))]
    if bad:
        raise ValueError(f"products file has unusable column names: {bad}")
    return unknown


def ingest_products(conn: sqlite3.Connection, csv_path: Path | str) -> int:
    frame = pd.read_csv(csv_path, dtype={"sku": str, "model_number": str})
    missing = [c for c in PRODUCT_FIELDS if c not in frame.columns]
    if missing:
        raise ValueError(f"products file missing columns: {missing}")

    extra = extra_product_columns(frame)
    for column in extra:
        conn.execute(f"ALTER TABLE products ADD COLUMN {column} TEXT")

    fields = PRODUCT_FIELDS + extra
    statement = (f"INSERT INTO products ({', '.join(fields)}) "
                 f"VALUES ({', '.join('?' for _ in fields)})")
    count = 0
    for line, record in enumerate(frame[fields].to_dict("records"), start=2):
        try:
            conn.execute(statement, [_cell(record[f]) for f in fields])
        except sqlite3.IntegrityError as exc:
            # The master is not a landing file. A bad price row is reported and
            # the page loads; a bad product row makes every comparison below it
            # describe something else, so this stops.
            raise ValueError(
                f"products file line {line} (sku {record['sku']}) was refused: "
                f"{exc}") from exc
        count += 1
    conn.commit()
    return count


def extra_price_columns(frame: pd.DataFrame) -> list[str]:
    """Observation columns the landing file carries that the schema does not.

    The same reasoning as for the product master, and the same guard. A field
    a capture starts recording mid-window, or one a feed returns, reaches the
    page on the next reload instead of being dropped at ingest.
    """
    unknown = [c for c in frame.columns if c not in PRICE_CSV_COLUMNS]
    bad = [c for c in unknown if not SAFE_COLUMN.match(str(c))]
    if bad:
        raise ValueError(f"prices file has unusable column names: {bad}")
    return unknown


def ingest_prices(conn: sqlite3.Connection, csv_path: Path | str) -> IngestResult:
    frame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    missing = [c for c in PRICE_CSV_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"prices file missing columns: {missing}")

    extra = extra_price_columns(frame)
    for column in extra:
        conn.execute(f"ALTER TABLE price_snapshots ADD COLUMN {column} TEXT")

    fields = PRICE_FIELDS + extra
    statement = (f"INSERT INTO price_snapshots ({', '.join(fields)}) "
                 f"VALUES ({', '.join('?' for _ in fields)})")

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
        # Blank is a fact: the page showed no such figure. Text that is not a
        # number is not, and coercing it to NULL fakes that fact.
        unreadable = None
        for column in MONEY_COLUMNS:
            cell = _cell(record[column])
            if cell is None:
                values.append(None)
                continue
            amount = pd.to_numeric(cell, errors="coerce")
            if pd.isna(amount):
                unreadable = f"{column} {str(cell)!r} is not a number"
                break
            values.append(float(amount))
        if unreadable is not None:
            rejected.append(Rejection(line, sku, raw, unreadable))
            continue
        values += [
            _cell(record["availability"]),
            _cell(record["seller"]),
            _cell(record["stock_hint"]),
            _cell(record["note"]),
        ] + [_cell(record[column]) for column in extra]

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
    # Every column, so one the schema does not name still reaches the page.
    frame = pd.read_sql_query(
        "SELECT * FROM price_snapshots ORDER BY captured_at, sku",
        conn, dtype={"sku": str})
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
