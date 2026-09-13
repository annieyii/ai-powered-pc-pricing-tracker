# PC Pricing Tracker (Best Buy)

Built as a take-home assessment submission.

## What it does

It tracks the listed price of four comparable 14-inch Windows Copilot+ PCs sold by
Best Buy, and compares the two that match on a stated equivalence rule. The question
it is built to answer is narrow on purpose: where does a Lenovo SKU sit against a
like-for-like competitor SKU, and is that position moving.

Running the app gives you a Streamlit page with a headline result and six numbered
sections, in this order:

1. **Price over time** for the strict equivalence group. This is the trend chart.
2. **Observation summary**, a short written read of what the figures show. Every
   number in it is checked against the computed values before it is displayed.
3. **Latest observation per product**, one row per SKU with its own capture time.
4. **Strict group matched-pair observation**, the price difference between the two
   strict SKUs at the most recent capture time where both carried a usable price.
5. **Reference products**, listed for context and excluded from the chart and the
   comparison above.

Prices are recorded by hand. Each observation is appended to
`data/structured/prices_manual.csv`, which is the system of record. On every page
load the app rebuilds an in-memory SQLite database from the two CSVs, and that
database is what refuses bad rows. Nothing is called on page load, so the app starts
with no API key and no model available. Two buttons do call a model when a reader
presses them; both are disabled when no endpoint is configured, and both sections
fall back to deterministic output.

## Compared products and how they were chosen

Selection was **anchor first**. Rather than picking three machines that look similar
and hoping they group, the starting point was a single Lenovo SKU that Best Buy sells
first-party and had in stock. Its specification was read off the product page, the
equivalence rule was derived from that specification, and competitors were then
searched for against the rule. This ordering matters, because a rule written after
looking at all the candidates can always be bent until the candidates fit.

**Equivalence fields.** Two SKUs are `strict` only if all of these match exactly:
processor model, memory, storage, screen size, operating system, device type, form
factor. The brief names six of these. Screen size is added here, because a rule that
lets a 14-inch and a 16-inch machine into the same group is comparing chassis sizes
as well as prices. The extra field only narrows the group; it never widens it.

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

**253 tests pass** at the time of writing: store behaviour including every refusal
path, the pure metric functions, the extraction gates against a stubbed client, and
seventeen end-to-end checks that drive the real Streamlit page against the real data
files. No test reaches the network.

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
4. Reload the Streamlit page. The CSV loader is deliberately not wrapped in
   `st.cache_data`, so a reload reflects the file as it now stands. Results a reader
   asked a model for are held in session state until the selection changes.

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

AI sits in **two** places in this repository, doing two different jobs. They are not
the same feature at two sizes, and only one of them is product facing.

### 1. Analysis on the dashboard (the product-facing feature)

Section 2 of the app is a short observation summary written in natural language:
what the window covers, what each product is listed at now, what the matched pair
shows, and whether anything moved. It is the feature a product manager would
actually use, because it answers *what changed and what should I look at* without
making them read the chart and the table first.

This is where a model belongs, and the reason is structural. **Acquisition ambiguity
ends when an official structured feed exists**: an API that returns `1299.99` does not
need a model to read `$1,299.99` off a page. **Interpretation ambiguity does not end,
and it grows with the row count.** Four SKUs over three days can be read by eye. Four
hundred SKUs over a quarter cannot, and no amount of structure in the feed makes the
reading easier. So the durable place for the model is the layer that turns computed
figures into a judgement about what matters, not the layer that turns text into
figures.

**How it is kept honest.** [`tracker/summarise.py`](tracker/summarise.py) hands the
model no rows and no page text. It builds a context dictionary of values `metrics.py`
has already computed, and the prompt states that no figure outside that dictionary
may appear in the reply. Every numeric token in the returned prose is then checked
back against the same dictionary. **If any figure is not there, the whole summary is
discarded**, not edited: a reader cannot tell which sentence was invented, so a
partially trusted note is worth less than a deterministic one. One retry is allowed,
naming the figures that were refused, and then the deterministic `template_summary`
takes over.

`template_summary` is the floor, not an error path. It is built by string formatting
from the same context, so it needs no endpoint, no key and no network, and it is what
renders when nothing else is present. The app reads a previously generated summary
from `data/summary.md` when one exists, flags it visibly if snapshots have been
recorded since it was written, and calls an endpoint only when someone presses
**Regenerate with the language model**. That button is disabled, naming the missing
variable, when `LLM_BASE_URL`, `LLM_MODEL` or `LLM_API_KEY` is unset. **The page still
loads and still summarises with none of them set and no network available.**

Generate the stored file by hand:

```bash
uv run python -m tracker.summarise
```

It writes `data/summary.md` only from output that passed the grounding check, with a
header recording the model, the generation time and the last capture time it saw.

### 2. Offline specification extraction (a worked example of validation)

[`tracker/extract.py`](tracker/extract.py) turns raw Best Buy page text into the
structured fields the equivalence rule needs. The same specification reaches the page
as `16GB Memory`, `16 GB RAM` and `System Memory: 16GB`, and titles routinely
disagree with the table below them.

Three gates stand between that model and any conclusion. Its reply is parsed through
a pydantic schema, so a malformed or invented shape fails at the boundary instead of
entering the data. The parsed values then meet deterministic validators: grounding (a
number the raw page text never contained was guessed, not read), plus range,
power-of-two memory, known-storage-size and form-factor enum checks. What survives is
scored field by field against `data/structured/products.csv`, which was filled in by
hand for all four products and is therefore verified ground truth. That gives a
stated denominator rather than a favourable example: N fields extracted across 4
products, X matched the verified value, Y validator findings. Results go to
`data/extraction_review.csv` with both values side by side, and the correction is
left to a person. The model never writes to `products.csv`, because letting
extraction edit the master would destroy the only reference the score has.

**This layer is retained as a demonstration, and it is worth being explicit about
what that means: it would not exist against the official structured API.** Best Buy
publishes a products API that returns these fields already typed. Given access to it,
the correct decision is to delete this module and call the endpoint, because reading
a specification out of prose is a workaround for not having the data, not a capability
worth keeping. It is here because this prototype does not have that access, and
because the validation pattern it demonstrates, scoring model output field by field
against a human-verified master, is the part that transfers to the analysis layer
above.

Run it by hand, never from the app. Nothing loads `.env` for you, so the three
variables have to reach the process; `set -a` exports everything the file sets:

```bash
set -a; source .env; set +a      # or export the three variables yourself
uv run python -m tracker.extract
```

It reads `data/raw_specs/<sku>.txt`, one verbatim copy of each product page's title
and Specifications block, and writes `data/extraction_review.csv`.

**What the run produced.** The same file was extracted twice, against two endpoints,
changing only the three variables in `.env`:

| Endpoint | Model | Matched the verified value | Ungrounded values |
|---|---|---|---|
| `https://api.openai.com/v1` | `gpt-4o-mini` | 39 of 48 | 0 |
| `http://localhost:11434/v1` | `qwen2.5:7b` (Ollama) | 41 of 48 | 0 |

`data/extraction_review.csv` holds the first, `data/extraction_review.qwen2.5-7b.csv`
the second. Neither run produced a number that was absent from the page it was given.

Read the misses before the totals, because they are not one kind of thing. Ten of the
twelve fields were 4 of 4 on both runs. Every miss is `model_name` or `cpu`:

- `model_name`, 0 of 4 on both. The prompt says to copy from the page; the master
  holds a shortened name that appears nowhere on the page. The two can never agree,
  so this measures a scoring convention, not an extraction failure.
- `cpu`, 1 of 4 and 0 of 4. The page splits the value: `Processor Model` is a family
  label, `Processor Model Number` carries the model. Only the Dell page states the
  whole value in one field, and that is the one `qwen2.5:7b` got right.
- One real error: `gpt-4o-mini` called the Dell a 2-in-1. That page has no
  `2-in-1 Design` row at all, and its title does not say 2-in-1, so the value was
  inferred. **No validator caught it**, because `2-in-1` is grounded nowhere but is a
  member of the allowed enum. The human-verified master caught it, and nothing else
  would have.

The 39 against 41 is therefore not a ranking of the two models. It is sensitive to a
rubric that the prompt does not encode. The prompt was left as written rather than
tuned after seeing the scores: with one labelled set, a prompt changed to raise the
score stops measuring anything.

### What no model does here

- It never parses a price. Every figure is read off the page by a person and into the
  CSV, and every computation over those figures is deterministic. The summary model is
  handed prices that were already computed, and `data/summary.md` repeats them, so it
  does restate a price; what it cannot do is arrive at one.
- It never decides equivalence. `role` is assigned by hand after checking the seven
  fields, and the code then applies that label. The check is a person's, not a rule
  the program enforces.
- It never writes to the system of record. Both CSVs stay human-edited.
- Nothing calls an endpoint on page load. A reviewer can run the whole app with no
  API key and no model available.

The client targets the **OpenAI-compatible chat-completions API** in both places, and
the endpoint is supplied at run time rather than assumed. The same code runs against a
remote endpoint or a local Ollama instance, with no backend-specific branch.
`LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` are read from the environment, and both
entry points refuse to fall back to a default that may not exist. See
[`.env.example`](.env.example).

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
| D. Brief explanation, 1 to 2 pages | `docs/report.md`. Full specification in `docs/spec.md`; sampling procedure in `docs/capture-checklist.md` |
| E. AI usage and human validation summary | "Where AI is used" above, `tracker/extract.py`, and `data/extraction_review.csv` (not yet generated) |
