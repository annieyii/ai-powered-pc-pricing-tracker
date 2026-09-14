# PC Pricing Tracker (Best Buy)

Tracks the listed price of four comparable 14-inch Windows Copilot+ PCs on Best Buy,
and compares the two that match on a stated equivalence rule. The question it is built
to answer is narrow on purpose: where does one SKU sit against a like-for-like
competitor, and is that position moving.

The reasoning behind the product choice, the equivalence rule, the assumptions and the
limitations is in the written explanation, not here. This file is how to run it.

## What it does

Running the app gives you a Streamlit page with a headline result and six numbered
sections, in this order:

1. **Price over time**, the trend chart. Every tracked product is plotted: the
   strict pair in full, the reference SKUs muted and marked in the legend.
2. **What the observations say**, the findings detected between consecutive
   captures, price and otherwise.
3. **Observation summary**, a short written read of what the figures show. Every
   figure in it appears among the computed values before it is displayed.
4. **Latest observation per product**, one row per selected SKU with its own
   capture time.
5. **Strict group matched-pair observation**, the price difference between the two
   strict SKUs at the most recent capture time where both carried a usable price.
6. **Reference products**, listed for context and excluded from the price
   difference above. They are on the chart, muted, because a reader asks where the
   other tracked machines sit.

Prices are recorded by hand. Each observation is appended to
`data/structured/prices_manual.csv`, which is the system of record. On every page
load the app rebuilds an in-memory SQLite database from the two CSVs, and that
database is what refuses bad rows. Nothing is called on page load, so the app starts
with no API key and no model available. Two buttons do call a model when a reader
presses them; both are disabled when no endpoint is configured, and both sections
fall back to deterministic output.

## Running it

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```bash
uv sync
uv run streamlit run tracker/app.py
```

That is the whole setup for reviewing the tracker. There is no database file to
create, no environment variable to set, and no API key: the SQLite store is built in
memory from the CSVs when the page loads, and every section renders deterministically.

The page carries two optional model buttons. Without an endpoint they are disabled and
say which variable is missing, which is the expected state rather than a failure. To
enable them, put the three values in `.env` and export them before starting the app,
because nothing in this project reads `.env` for you:

```bash
set -a; source .env; set +a      # or export the three variables yourself
uv run streamlit run tracker/app.py
```

Tests:

```bash
uv run pytest
```

**278 tests pass** at the time of writing: store behaviour including every refusal
path, the pure metric functions, the extraction gates against a stubbed client, and
twenty-six end-to-end checks that drive the real Streamlit page against the real data
files. No test reaches the network.

## Updating the data

Adding an observation is a five-minute loop and needs no code change.

1. Open each product URL from `data/structured/products.csv`, with the store still
   set to Union Square, NYC.
2. Record seven fields per SKU: `price`, `regular_price`, `savings`, `availability`,
   `seller`, `stock_hint`, `pickup_eta`. `pickup_eta` is `today` or a date such as
   `2026-09-18`; `today` is a state, not the capture date, so same-day pickup on two
   different days does not read as a change. `price` means the clearly labelled purchase price for that
   SKU, that seller and that fulfilment state. It is not the largest number on the
   page, not a monthly financing figure, not a comparison value, and not an open-box
   range. If a field is not shown, leave it blank. Never guess.
3. Append one row per SKU to `data/structured/prices_manual.csv`, with
   `captured_at_local` in `YYYY-MM-DDTHH:MM` Asia/Taipei time.
4. Reload the Streamlit page. The CSV loader is deliberately not wrapped in
   `st.cache_data`, so a reload reflects the file as it now stands. Results a reader
   asked a model for are held in session state until the selection changes.

If a row is malformed or breaks one of the rules the SQL schema holds (unknown SKU,
duplicate capture time, a seller other than Best Buy, a purchasable listing with no
price, savings that do not reconcile with the two prices), it appears at the top of
the page in an expanded panel with its **line number in the CSV and the database's
own reason for refusing it**. The row is left in the file untouched. The rest of the
data still loads.

Why capture times are clustered around the Best Buy weekly-ad changeover and around a
stated promotion expiry, rather than spaced evenly, is part of the written explanation.

## Where things are

| What | Where |
|---|---|
| The tracker | `tracker/app.py`, over `data/structured/products.csv` and `data/structured/prices_manual.csv` |
| The trend chart | Section 1 of the app, first on the page. Data from `tracker/metrics.py:series_for` |
| Source and update steps | `tracker/`, `tests/`, and the "Running it" and "Updating the data" sections above |
| Offline extraction and its score | `tracker/extract.py`, `data/extraction_review.csv` and `data/extraction_review.qwen2.5-7b.csv`, one run per endpoint |
