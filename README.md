# PC Pricing Tracker (Best Buy)

Built as a take-home assessment submission.

## What it does

It tracks the listed price of four comparable 14-inch Windows Copilot+ PCs sold by
Best Buy, and compares the two that match on a stated equivalence rule. The question
it is built to answer is narrow on purpose: where does a Lenovo SKU sit against a
like-for-like competitor SKU, and is that position moving.

Running the app gives you a Streamlit page with four sections, in this order:

1. **Price over time** for the strict equivalence group. This is the trend chart.
2. **Latest observation per product**, one row per SKU with its own capture time.
3. **Strict group matched-pair observation**, the price difference between the two
   strict SKUs at the most recent capture time where both carried a usable price.
4. **Reference products**, listed for context and excluded from the chart and the
   comparison above.

Prices are recorded by hand. Each observation is appended to
`data/structured/prices_manual.csv`, which is the system of record. On every page
load the app rebuilds an in-memory SQLite database from the two CSVs, and that
database is what refuses bad rows. Nothing in the running app calls a language model,
so it starts with no API key and no model available.

## Compared products and how they were chosen

Selection was **anchor first**. Rather than picking three machines that look similar
and hoping they group, the starting point was a single Lenovo SKU that Best Buy sells
first-party and had in stock. Its specification was read off the product page, the
equivalence rule was derived from that specification, and competitors were then
searched for against the rule. This ordering matters, because a rule written after
looking at all the candidates can always be bent until the candidates fit.

**Equivalence fields.** Two SKUs are `strict` only if all of these match exactly:
processor model, memory, storage, screen size, operating system, device type, form
factor. These are the grouping fields named in the assessment brief.

| Role | Product | SKU | CPU | Form factor | List price |
|---|---|---|---|---|---|
| strict | Lenovo Yoga 7a 2-in-1 14in | 6672159 | AMD Ryzen AI 5 430 | 2-in-1 | $1,299.99 |
| strict | HP OmniBook X Flip 2-in-1 14in | 6668002 | AMD Ryzen AI 5 430 | 2-in-1 | $1,299.99 |
| reference | HP OmniBook X Flip 2-in-1 14in | 6667986 | Intel Core Ultra 5 325 | 2-in-1 | $1,349.99 |
| reference | Dell 14S 14in | 6679150 | AMD Ryzen AI 5 430 | clamshell | $1,299.99 |

All four are sold and shipped by Best Buy and were purchasable at capture time. The
store is fixed to Union Square, NYC for every capture. Currency is USD.

**Why two are `strict`.** SKUs 6672159 and 6668002 match on every field in the
equivalence rule, and were listed at the same price at the first capture. They are
the only two that enter the trend chart and the price-difference metric.

They are not identical machines, and the README will not pretend otherwise: outside
the grouping key, **panel brightness is 400 nits on the Lenovo and 300 nits on the
HP**. The grouping key does not cover brightness, so this difference is carried
openly rather than grouped away. It is one reason the comparison is reported as a
matched-pair observation and not as an estimate of anything.

**Why two are `reference`.** SKU 6667986 is the Intel build of the same HP chassis,
so processor model differs and so does list price. SKU 6679150 is a clamshell rather
than a 2-in-1, and its display is LED rather than OLED. Each differs from the strict
pair on more than one field, so nothing about either can be attributed to a single
attribute. They are shown because they are useful context for a pricing reader, and
they are kept out of the chart and the metric for the same reason.

**Two SKUs the rule rejects.** Recorded so that the rule can be seen to exclude as
well as include:

- **SKU 6616071**, Dell Inspiron 14 2-in-1, Core 5 120U. Wrong processor generation.
- **SKU 6687840**, HP EliteBook X G2i. Wrong processor tier and wrong memory.

Both are 14-inch Windows 2-in-1s and would pass a loose eyeball test. They fail the
stated rule, so they are out.

## Running it

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```bash
uv sync
uv run streamlit run tracker/app.py
```

That is the whole setup. There is no database file to create, no environment variable
to set, and no API key. The SQLite store is built in memory from the CSVs when the
page loads.

Tests:

```bash
uv run pytest
```

**99 tests pass** at the time of writing: store behaviour including every refusal
path, the pure metric functions, the extraction gates against a stubbed client, and
five end-to-end checks that drive the real Streamlit page against the real data files.
No test reaches the network.

## Updating the data

Adding an observation is a five-minute loop and needs no code change.

1. Open each product URL from `data/structured/products.csv`, with the store still
   set to Union Square, NYC.
2. Record six fields per SKU: `price`, `regular_price`, `savings`, `availability`,
   `seller`, `stock_hint`. `price` means the clearly labelled purchase price for that
   SKU, that seller and that fulfilment state. It is not the largest number on the
   page, not a monthly financing figure, not a comparison value, and not an open-box
   range. If a field is not shown, leave it blank. Never guess.
3. Append one row per SKU to `data/structured/prices_manual.csv`, with
   `captured_at_local` in `YYYY-MM-DDTHH:MM` Asia/Taipei time.
4. Reload the Streamlit page. There is no caching in the app, deliberately, so a
   reload reflects the file as it now stands.

If a row is malformed or violates one of the rules below, it appears at the top of
the page in an expanded panel with its **line number in the CSV and the database's
own reason for refusing it**. The row is left in the file untouched. The rest of the
data still loads.

The sampling procedure, including why capture times are clustered around the Best Buy
weekly-ad changeover rather than spaced evenly, is in
[`docs/capture-checklist.md`](docs/capture-checklist.md).

## What the data rules are and where they live

The rules live in the SQL schema in
[`tracker/store.py`](tracker/store.py), not in a chain of Python `if` statements. A
row that breaks one is refused by the database, and the refusal comes back carrying
the database's own message, which is then shown in the UI. The relevant constraints:

```sql
sku TEXT NOT NULL REFERENCES products(sku)          -- a price for an unknown SKU
PRIMARY KEY (sku, captured_at)                      -- the same SKU twice at one time
CHECK (availability IN ('Add to cart', 'Unavailable', 'Sold Out'))
CHECK (seller = 'Best Buy')                         -- third-party listings are not the same offer
CHECK (price IS NULL OR (price > 0 AND price < 10000))
CHECK (savings IS NULL OR regular_price IS NULL OR price IS NULL
       OR abs(savings - (regular_price - price)) < 0.02)   -- savings must reconcile
CHECK (availability <> 'Add to cart' OR price IS NOT NULL) -- purchasable implies priced
```

The last one came from an observed failure mode: a listing that is no longer
purchasable can still display a stale price with no live offer behind it.

**The CSVs stay the system of record.** The SQLite store is rebuilt in memory on every
load and is never persisted. That is a deliberate choice, not laziness. An insert-only
database that survived a reload would keep serving a price the CSV had since corrected,
and would keep a row the CSV had since deleted. Because the store is rebuilt, a
correction or a deletion made in a CSV applies on the next page load, and the git
diff of the CSV remains a readable audit trail of what was observed and when.

Three rules that are never relaxed:

1. **Append only.** A correction is a new row with a note, never an edit of an old one.
2. **Never back-fill.** A missing price stays missing. Not the previous value, not zero.
3. **Report, do not repair.** A row the database refuses is surfaced with the reason.
   The program never rewrites data to make it fit.

## Where AI is used

AI is used in exactly one place: **offline structured extraction of product
specifications from raw Best Buy page text**, on the product-onboarding path. It is
not on the price path and not in the dashboard's request path.

This is the part of the problem that is genuinely unstructured. The same specification
appears on different pages as `16GB Memory`, `16 GB RAM` and `System Memory: 16GB`,
and product titles routinely disagree with the specification table below them. Turning
that text into the structured fields the equivalence rule needs is work a model is
good at, and work that is tedious and error-prone by hand.

What the model does **not** do is just as much of the design:

- It never reads or produces a price. Price parsing is deterministic.
- It never decides equivalence. Grouping is a rule applied to structured fields.
- It is never called by the dashboard. **The app has no runtime LLM dependency**, which
  is why a reviewer can run it with no API key and no model pulled.

**How the output is checked.** [`tracker/extract.py`](tracker/extract.py) puts three
gates between the model and any conclusion. Its reply is parsed through a pydantic
schema, so a malformed or invented shape fails at the boundary instead of entering the
data. The parsed values then meet deterministic validators: grounding (a number the
raw page text never contained was guessed, not read), plus range, power-of-two memory,
known-storage-size and form-factor enum checks. What survives is scored field by field
against `data/structured/products.csv`, which all four products were captured by hand
to fill in, so it is a human-verified ground truth. That gives a stated denominator
rather than a favourable example: N fields extracted across 4 products, X matched the
verified value, Y validator findings.

The model never writes to `products.csv`. Letting extraction edit the master would
destroy the only reference the score has. Results go to `data/extraction_review.csv`
with both values side by side, and the correction is left to a person.

The client targets the **OpenAI-compatible chat-completions API**, and the endpoint is
supplied at run time rather than assumed. The same code runs against a remote endpoint
or a local Ollama instance, with no backend-specific branch. `LLM_BASE_URL`,
`LLM_MODEL` and `LLM_API_KEY` are read from the environment, and the extraction step
exits naming the missing variable rather than falling back to a default that may not
exist. See [`.env.example`](.env.example).

Run it by hand, never from the app:

```bash
uv run python -m tracker.extract
```

It reads `data/raw_specs/<sku>.txt`, one verbatim copy of each product page's title
and Specifications block, and writes `data/extraction_review.csv`.

> **[Placeholder]** The raw specification files have not been captured yet, so
> `data/extraction_review.csv` has not been generated. The figures for N, X and Y
> above are to be filled in from that file once the run is made.

AI coding assistance was also used while building the repository itself. Those commits
carry a `Co-Authored-By` trailer, so the extent of it is visible in `git log`.

## Assumptions and limitations

**Assumptions**

1. The store is fixed to Union Square, NYC for every capture. US market, USD.
2. Best Buy online pricing is national; availability signals are store-dependent.
3. Where a title and the specification table disagree, the specification table wins,
   except where a more granular field contradicts a categorical one, in which case
   the granular field wins.
4. Capture times are Asia/Taipei, recorded as `YYYY-MM-DDTHH:MM`.

**Limitations**

1. **The observation window is short.** It is enough to demonstrate that the tracker
   records movement and updates; it is not enough to establish a price trend. The
   dashboard reports its own snapshot count and last observation time from the data,
   so what is on screen is always the true state of the file.
2. **No historical data exists.** The Internet Archive holds no snapshot of any of
   these SKUs, so the series can only start when manual capture started.
3. **Captures are manual and therefore sparse**, not continuous. A price that moves
   and moves back between two captures is invisible to this tool.
4. The two strict SKUs differ on panel brightness, which sits outside the grouping key.
5. Best Buy's own `Processor Model` field was found to be wrong for at least one SKU,
   so processor generation is determined from `Processor Model Number` instead.
6. All four SKUs showed shipping unavailable and pickup only at the fixed store. That
   may be specific to that store rather than a national condition.
7. The comparison between the two strict SKUs is a **matched-pair observation at a
   single point in time**. It is not an estimate of anything attributable to brand,
   processor platform or form factor.
8. All four products carry a list price of $1,299.99 except the Intel variant at
   $1,349.99. Where a current price sits below list, that is promotional state. It is
   not read as a consequence of form factor or brand.
9. The dataset contains no personal data. All of it is public product information.

> **[Placeholder]** Final snapshot count, observation window and any observed price
> movements are to be stated here once the scheduled captures in
> `docs/capture-checklist.md` are complete.

### A note on how prices are collected

Prices are captured manually, and no automated retrieval from Best Buy is performed.
Best Buy's Terms and Conditions prohibit scraping and automated navigation. Browser
automation was evaluated and was not implemented. This is a prototype constraint and
**not a legal determination**: production use of this approach would require an
authorised feed or API, and organisational review before any such decision is made.
The manual path also has an incidental benefit at this scale, which is that a human
reads the page and can tell a purchase price from a financing figure.

---

## Where each deliverable lives

| Required deliverable | Where it is in this repository |
|---|---|
| A. Working tracker or output file | `tracker/app.py`, over `data/structured/products.csv` and `data/structured/prices_manual.csv` |
| B. Updateable pricing trend chart | Section 1 of the app, first on the page. Data from `tracker/metrics.py:series_for` |
| C. Source code, formulas, or automation steps | `tracker/`, `tests/`, and the "Running it" and "Updating the data" sections above |
| D. Brief explanation, 1 to 2 pages | Separate PDF. Full specification in `docs/superpowers/spec.md`; sampling procedure in `docs/capture-checklist.md` |
| E. AI usage and human validation summary | "Where AI is used" above, `tracker/extract.py`, and `data/extraction_review.csv` (not yet generated) |
