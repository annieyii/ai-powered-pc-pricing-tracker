# PC Pricing Tracker (Best Buy)

Tracks the listed price of four comparable 14-inch Windows Copilot+ PCs on Best Buy and compares the two that match on a stated equivalence rule. The question is narrow on purpose: where does one SKU sit against a like-for-like competitor, and is that position moving.

Prices are recorded by hand into an append-only CSV. Every page load rebuilds an in-memory SQLite database from it, and the schema is what refuses a bad row.

The reasoning behind the product choice, the equivalence rule, the assumptions and the limitations is in the written explanation. This file is how to run it.

## Running it

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```bash
uv sync
uv run streamlit run tracker/app.py     # the dashboard
uv run pytest                           # 281 tests, none reaching the network
```

That is the whole setup: no database to create, no environment variable, no API key. Every section renders deterministically without one.

Two optional buttons on the page call a language model. Without an endpoint they are disabled and name the variable that is missing, which is the expected state rather than a failure. To enable them, fill in `.env` and export it, because nothing here reads `.env` for you:

```bash
set -a; source .env; set +a
uv run streamlit run tracker/app.py
```

`tracker/extract.py` is an offline extraction run, not part of the page:

```bash
set -a; source .env; set +a
uv run python -m tracker.extract
```

## Adding an observation

No code change is needed.

1. Open each `source_url` from `products.csv`, store still set to Union Square, NYC.
2. Record seven fields per SKU into `prices_manual.csv`, with `captured_at_local` as `YYYY-MM-DDTHH:MM` in Asia/Taipei.
3. Reload the page. The loader is deliberately not cached, so it reflects the file.

| Field | What it is |
|---|---|
| `price` | The clearly labelled purchase price. Not the largest number on the page, not a monthly financing figure, not a comparison value, not an open-box range |
| `regular_price` | The struck-through or comparison price, blank when there is no promotion |
| `savings` | The stated saving, blank when there is none |
| `availability` | `Add to cart`, `Unavailable` or `Sold Out` |
| `seller` | The "Sold by" text |
| `stock_hint` | The stock note as written, blank if none |
| `pickup_eta` | `today`, or a date like `2026-09-18`. `today` is a state, not the capture date |

If a field is not shown, leave it blank. Never guess.

A row the schema refuses (unknown SKU, duplicate capture time, a seller other than Best Buy, a purchasable listing with no price, savings that do not reconcile) appears at the top of the page with its CSV line number and the database's own reason. It is left in the file untouched, and the rest of the data still loads.

## Layout

```
tracker/
  app.py          Streamlit. The only module that imports streamlit
  store.py        SQLite schema, ingest, queries. The only module that knows storage
  metrics.py      Pure functions over DataFrames. No streamlit, no path, no network
  insights.py     Scope, deterministic change detection, scoring, reading order
  summarise.py    Summary context, prose generation, grounding check
  extract.py      Offline specification extraction, scored against the master
  capture.py      Prompts for a capture session and appends it
  bestbuy.py      Products API adapter. Not integrated and not live-tested
data/
  structured/
    products.csv          Product master, verified by hand
    prices_manual.csv     Append-only landing file, one row per SKU per capture
  raw_specs/<sku>.txt     Verbatim page text, the input to extraction
  extraction_review.<model>.csv
                        Extraction scored field by field against the master,
                        one file per endpoint the run was pointed at
  summary.md              Stored dashboard summary, written only by
                        `python -m tracker.summarise`. Absent until then,
                        and the page computes its own instead
tests/                    One file per module
```

Adding a column to either CSV needs no code change: the store carries columns the schema does not name, the sidebar grows a filter for any column the products disagree about, and the change detector watches it from its first change.
