"""Streamlit dashboard.

The only module that imports streamlit. There is deliberately no caching:
the documented workflow is to append a row to the CSV and reload, and
st.cache_data keys on the arguments rather than the file contents, so a cached
loader would keep serving the old file.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from typing import Any

# Streamlit executes this file directly, so the interpreter puts tracker/ on
# sys.path rather than the project root and `import tracker.x` fails. An
# editable install is supposed to cover that, but making the entry point depend
# on install state means `streamlit run` breaks for anyone who unpacked the
# project without syncing, or whose path defeats the .pth mechanism. Four lines
# here make it work either way.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tracker.insights import (
    CHANGE_KINDS,
    KIND_GROUPS,
    Scope,
    detect,
    prefer,
    select_by_score,
    select_with_model,
)
from tracker.metrics import (
    latest_per_sku,
    series_for,
    strict_comparison,
    strict_time_points,
)
from tracker.store import build
from tracker.summarise import (
    build_context,
    build_client,
    read_stored_summary,
    settings_or_reason,
    stored_is_stale,
    summarise,
    template_summary,
    write_stored_summary,
)

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS_CSV = ROOT / "data" / "structured" / "products.csv"
PRICES_CSV = ROOT / "data" / "structured" / "prices_manual.csv"

# Two identical series must stay separable without moving either line off its
# real value. A matched pair at parity overlaps exactly, which is the normal
# case here rather than the edge case, so the trace drawn second is dashed,
# hollow and smaller: the one underneath shows through the gaps and through the
# marker centres instead of disappearing beneath it. Colours are set explicitly
# because the inherited second colour was too pale to find on a white ground.
def usd(text: str) -> str:
    """Escape dollar signs so Streamlit does not read a price pair as LaTeX.

    Two unescaped `$` in one markdown block make everything between them a
    maths span: `$999.99 ... $1,299.99` renders as green italics with the
    currency symbols eaten. Every figure on this page is a price, so escaping
    is the default rather than the exception.
    """
    return text.replace("$", r"\$")


# Attribute filters are only another way of choosing SKUs, so they feed the
# same `Scope(skus=...)` that the product picker does and nothing downstream
# changes. A column whose values are all the same is not a choice, so it grows
# a control only once the catalogue disagrees about it.
#
# The set of filters is read off the product master rather than listed here.
# A column added to products.csv, whether by hand or by an API that returns
# more attributes than this one does, becomes a filter on the next reload with
# no edit to this file. That is the whole extension story for the sidebar.

#: Columns that identify a product rather than describe it. Filtering by these
#: would duplicate the product picker or offer one option per row.
NOT_FILTERABLE = frozenset({"sku", "model_name", "model_number", "source_url"})

#: The order a reader reaches for, for the columns this project already knows.
#: Anything not named here still appears, after these, in the master's own
#: column order. Nothing is hidden for being unrecognised.
FILTER_ORDER = ["role", "brand", "cpu", "form_factor"]

#: Labels worth spelling properly. Any other column is title-cased from its
#: own name, so a new column is readable before anyone writes it down here.
FILTER_LABELS = {
    "cpu": "Processor", "ram_gb": "Memory (GB)", "storage_gb": "Storage (GB)",
    "screen_inch": "Screen (in)", "brightness_nits": "Brightness (nits)",
    "display_type": "Display", "screen_resolution": "Resolution",
    "list_price": "List price", "operating_system": "Operating system",
    "device_type": "Device type", "form_factor": "Form factor",
    "touch_screen": "Touch screen",
}


def filterable_columns(products: pd.DataFrame) -> list[str]:
    """Every descriptive column, the four that lead first."""
    rest = [c for c in products.columns
            if c not in NOT_FILTERABLE and c not in FILTER_ORDER]
    return [c for c in FILTER_ORDER if c in products.columns] + rest


def filter_label(column: str) -> str:
    return FILTER_LABELS.get(column, column.replace("_", " ").capitalize())


COLOURS = ["#1f4e9c", "#d1495b", "#2a9d8f", "#6c757d"]

#: Reference SKUs are context, not comparison. They are on the chart because a
#: reader asks where the other tracked machines sit, and muted because reading
#: a difference off them would be attributing it to whichever field one
#: happens to notice.
#:
#: Muted, but not all the same muted: one grey for every reference SKU reads
#: as a single product until you hover, and the count of them is the thing
#: that grows. These stay low-contrast against the strict pair while staying
#: separable from each other.
REFERENCE_COLOURS = ["#9aa3ad", "#b0a08f", "#93a8a0", "#a79aad",
                     "#a8a26f", "#8f9fb3", "#b39a9a", "#8fa8a8"]

#: Past this many series a line chart stops being read and starts being
#: decoded. The filters are the answer, so the page says so rather than
#: silently drawing something unreadable.
CROWDED_CHART = 6
DASHES = ["solid", "dash", "dot", "dashdot"]
MARKERS = ["circle", "circle-open", "diamond-open", "x"]
SIZES = [13, 9, 9, 9]

st.set_page_config(page_title="PC Pricing Tracker", layout="wide")
st.title("PC Pricing Tracker — Best Buy")
st.caption("Manually verified observations. Store: Union Square, NYC. "
           "Currency: USD. Times are Asia/Taipei.")

data = build(PRODUCTS_CSV, PRICES_CSV)

if data.rejected:
    with st.expander(
            f"{len(data.rejected)} of {data.total} rows were refused", expanded=True):
        st.caption("Refused rows are left in the CSV untouched. Nothing is "
                   "repaired or back-filled.")
        st.dataframe(pd.DataFrame([{"line": r.line, "sku": r.sku,
                                    "reason": r.reason, **r.raw}
                                   for r in data.rejected]),
                     width="stretch", hide_index=True)

st.caption(f"{data.accepted} of {data.total} rows accepted")

if data.prices.empty:
    st.warning("No usable snapshots yet. Append rows to "
               "`data/structured/prices_manual.csv` and reload this page.")
    st.stop()

# --------------------------------------------------------------- selection
# The filter binds every section below it. A chart drawn for one selection
# sitting beside a summary written for another is the failure this guards
# against, and no grounding check would catch it: both halves are separately
# true. So the scope is chosen once and applied once.
names = dict(zip(data.products["sku"],
                 data.products["brand"] + " " + data.products["model_name"]))

eligible = data.products

#: Filters the reader has actually narrowed, label to chosen values. Kept so
#: an empty result can name what emptied it. With a dozen controls, "widen the
#: selection" is not an instruction, it is a search.
narrowed: dict[str, list[Any]] = {}


def option_counts(column: str, values: list[Any]) -> dict[Any, int]:
    """How many products each option would leave, given the other filters.

    Intersecting filters have dead ends: every choice is reasonable alone and
    the combination matches nothing. Cascading avoids that by removing options,
    which silently drops selections another control is still holding and makes
    the result depend on the order they were touched. Counting instead keeps
    every option reachable and puts the dead end where it can be seen before it
    is chosen: an option reading `(0)` is one that empties the page.

    The other filters are read from session state, which on a rerun already
    holds what the reader last chose.
    """
    others = data.products
    for other in filterable_columns(data.products):
        if other == column:
            continue
        chosen = st.session_state.get(f"filter_{other}")
        if chosen is not None:
            others = others[others[other].isin(chosen)]
    counted = others[column].value_counts()
    return {value: int(counted.get(value, 0)) for value in values}


def attribute_filter(column: str, label: str) -> None:
    """One control for one column, or none at all.

    Options come from the whole master rather than from what the other filters
    have already left, and carry the count they would leave.
    """
    global eligible
    if column not in data.products.columns:
        return
    values = sorted(data.products[column].dropna().unique().tolist())
    if len(values) < 2:
        return
    counts = option_counts(column, values)
    kept = st.multiselect(
        label, options=values, default=values, key=f"filter_{column}",
        format_func=lambda v: f"{v}  ({counts.get(v, 0)})")
    if set(kept) != set(values):
        narrowed[label] = kept
    eligible = eligible[eligible[column].isin(kept)]


with st.sidebar:
    st.header("Selection")

    if st.button("Reset filters", width="stretch"):
        for stale in [k for k in st.session_state if k.startswith("filter_")]:
            del st.session_state[stale]
        st.session_state.pop("products", None)
        # Not `lead_with`. That is a reading preference, not a filter: clearing
        # it here would throw away what the reader asked to see first every
        # time they widened the data they were looking at.
        st.rerun()

    # The window comes first: this is a tracker, so when is the outer question
    # and everything else narrows inside it.
    earliest = data.prices["captured_at"].min().date()
    latest = data.prices["captured_at"].max().date()
    window = st.date_input("Capture dates", value=(earliest, latest),
                           min_value=earliest, max_value=latest)

    every_sku = list(data.products["sku"])
    # Both HP SKUs carry the same brand and model name, so the CPU is what
    # makes the option readable. The prose below keeps the shorter name.
    cpus = dict(zip(data.products["sku"], data.products["cpu"]))
    picked = st.multiselect(
        "Products", options=every_sku, default=every_sku, key="products",
        format_func=lambda sku: f"{names.get(sku, sku)} · {cpus.get(sku, '')}")

    columns = filterable_columns(data.products)
    lead, rest = columns[:len(FILTER_ORDER)], columns[len(FILTER_ORDER):]

    for column in lead:
        attribute_filter(column, filter_label(column))

    # An expander closes on every rerun, so choosing something inside it made
    # the control vanish the moment it was used. It stays open while anything
    # inside it is narrowing the page.
    secondary_in_use = any(
        set(st.session_state.get(f"filter_{column}", []))
        != set(data.products[column].dropna().unique().tolist())
        for column in rest
        if f"filter_{column}" in st.session_state)

    with st.expander("More attributes", expanded=secondary_in_use):
        if not any(data.products[c].dropna().nunique() > 1 for c in rest):
            st.caption("Every product on record shares these, so there is "
                       "nothing here to choose between yet.")
        for column in rest:
            attribute_filter(column, filter_label(column))

    # Both kinds of control narrow, so they intersect. Choosing a brand and
    # then a product from another brand selects nothing, and the page says so
    # rather than quietly preferring one of the two.
    picked = [sku for sku in picked if sku in set(eligible["sku"])]

    # Not a filter. Readers arrive with different questions, and the score
    # cannot know which one; asking is cheaper and more honest than inferring,
    # and nothing is hidden either way.
    st.divider()
    st.caption("**Lead with** · reorders the findings, hides nothing")
    lead = st.multiselect("Lead with", options=list(KIND_GROUPS),
                          default=list(KIND_GROUPS), key="lead_with",
                          label_visibility="collapsed")

    st.caption(f"{len(picked)} of {len(data.products)} products match")
    if narrowed:
        st.caption("Narrowed by: " + ", ".join(
            f"{label} ({', '.join(str(v) for v in values) or 'nothing'})"
            for label, values in narrowed.items()))

start = end = None
# The full range is not a filter. Normalising it to None is what `skus` already
# does below, and without it the untouched page reports a selection it has not
# made: every key would carry a date and never compare equal to Scope().
if (isinstance(window, (list, tuple)) and len(window) == 2
        and tuple(window) != (earliest, latest)):
    start = datetime.combine(window[0], time.min)
    end = datetime.combine(window[1], time.max)

scope = Scope(skus=None if set(picked) == set(every_sku) else tuple(picked),
              start=start, end=end)
scoped = scope.apply(data.prices)
view = replace(data, prices=scoped)
filtered = scope.key() != Scope().key()

# A result computed for one scope must not survive a change of scope.
if st.session_state.get("scope_key") != scope.key():
    st.session_state["scope_key"] = scope.key()
    st.session_state.pop("summary_run", None)
    st.session_state.pop("ranking", None)

settings, missing_reason = settings_or_reason(os.environ)

if scoped.empty:
    if narrowed:
        st.warning(
            "The current selection contains no observations. These filters "
            "are narrowing it, and they combine: "
            + "; ".join(f"**{label}** kept "
                        f"{', '.join(str(v) for v in values) or 'nothing'}"
                        for label, values in narrowed.items())
            + ". Widening any one of them may be enough.")
    else:
        st.warning("The current selection contains no observations. Widen the "
                   "product or date choice in the sidebar.")
    st.stop()

if filtered:
    st.info(f"A selection is active: {len(scoped)} of {len(data.prices)} "
            f"observations are in view, and every section below reflects it.")

latest = latest_per_sku(scoped)
points = strict_time_points(scoped, data.products)
st.caption(f"Last observation {scoped['captured_at'].max():%Y-%m-%d %H:%M} · "
           f"{len(scoped)} snapshots · "
           f"{points} capture time(s) covering the strict group")

found = prefer(detect(scoped, data.products), lead)

# ------------------------------------------------------- headline result
# The question the page exists to answer, above the evidence for it. A reader
# who stops after one screen should still leave with the answer rather than
# with a chart they have to interpret first.
comparison = strict_comparison(scoped, data.products)
names = dict(zip(data.products["sku"],
                 data.products["brand"] + " " + data.products["model_name"]))

# A headline of prices alone says "nothing happened" in a window where the
# prices held and the availability did not. The count of everything else that
# moved sits beside them, so the first screen carries both halves.
movements = [i for i in found if i.kind in CHANGE_KINDS]
latest_at = scoped["captured_at"].max()
recent = [i for i in movements if i.facts.get("at") == f"{latest_at:%Y-%m-%dT%H:%M}"]

if comparison["comparable"]:
    columns = st.columns(len(comparison["rows"]) + 3)
    for column, row in zip(columns, comparison["rows"]):
        column.metric(names.get(row["sku"], row["sku"]), f"${row['price']:,.2f}")
    columns[-3].metric("Price difference", f"${comparison['delta']:,.2f}")
    columns[-2].metric("Capture times", points)
    columns[-1].metric(
        "Movements", len(movements),
        delta=(f"{len(recent)} at the latest capture" if recent else None),
        delta_color="off",
        help="Every change between consecutive captures in the selection, "
             "price or not: stock notes, pickup dates, availability. Listed "
             "in section 2.")
    if comparison["parity"]:
        st.success(usd(
            f"**The matched pair is at price parity.** Both SKUs were listed "
            f"at ${comparison['rows'][0]['price']:,.2f} at "
            f"{comparison['captured_at']:%Y-%m-%d %H:%M}, and they match on "
            f"every field the equivalence rule uses."))
    else:
        st.success(usd(
            f"**{names[comparison['cheaper_sku']]} is "
            f"${comparison['delta']:,.2f} below "
            f"{names[comparison['dearer_sku']]}** at "
            f"{comparison['captured_at']:%Y-%m-%d %H:%M}."))
else:
    st.warning(f"No matched-pair comparison yet: {comparison['reason']}.")

st.divider()

# --------------------------------------------------------------- 1. trend
st.header("1. Price over time")
st.caption("Every tracked product is plotted. The two that satisfy the "
           "equivalence rule are drawn in full; the two reference SKUs are "
           "muted, because each differs from the pair on more than one field "
           "and neither enters the price-difference metric.")

strict = data.products[data.products["role"] == "strict"]
# Strict first, so the pair takes the strong colours and sits on top of the
# context rather than under it.
ordered = pd.concat([strict, data.products[data.products["role"] != "strict"]])

figure = go.Figure()
for index, product in enumerate(ordered.itertuples()):
    line = series_for(scoped, product.sku)
    if line.empty:
        continue
    is_strict = product.role == "strict"
    name = f"{product.brand} {product.model_name}"
    if not is_strict:
        name += "  (reference)"
    colour = (COLOURS[index % len(COLOURS)] if is_strict
              else REFERENCE_COLOURS[index % len(REFERENCE_COLOURS)])
    figure.add_trace(go.Scatter(
        x=line["captured_at"], y=line["price"], mode="lines+markers", name=name,
        legendrank=index,
        line=dict(dash=DASHES[index % len(DASHES)],
                  width=2 if is_strict else 1, color=colour),
        marker=dict(symbol=MARKERS[index % len(MARKERS)],
                    size=SIZES[index % len(SIZES)] if is_strict else 7,
                    color=colour, line=dict(width=2, color=colour)),
        opacity=1.0 if is_strict else 0.55,
        hovertemplate="%{x|%b %d %H:%M}<br>$%{y:.2f}<extra>" + name + "</extra>",
    ))
figure.update_layout(yaxis_title="Price (USD)",
                     xaxis_title="Captured at (Asia/Taipei)",
                     hovermode="x unified", height=440,
                     legend=dict(orientation="h", y=-0.3))
st.plotly_chart(figure, width="stretch")

if len(figure.data) > CROWDED_CHART:
    st.caption(f"{len(figure.data)} series are plotted. A line chart stops "
               f"being readable somewhere around {CROWDED_CHART}; narrow the "
               f"sidebar by brand, processor or role to compare a few at a "
               f"time. The equivalence group is always the two drawn in full.")

# A reader who sees one line where the legend names two will not assume parity;
# they will assume the chart is broken. So the coincidence is stated.
priced = strict.merge(scoped, on="sku")
per_moment = priced.groupby("captured_at")["price"].agg(["nunique", "count"])
together = per_moment[per_moment["count"] == len(strict)]
if not together.empty and (together["nunique"] == 1).all():
    st.caption("Both series are drawn. They coincide at every capture time "
               "because the two SKUs were listed at the same price, so one "
               "line sits exactly on the other.")

if points < 2:
    st.info(f"Only {points} capture time recorded so far. A trend needs at "
            "least two.")
else:
    moved = strict.merge(scoped, on="sku").groupby("sku")["price"].nunique()
    if (moved <= 1).all():
        # "No price change" reads as "nothing happened", and in this window that
        # is false: the prices held while stock and availability moved. A chart
        # of prices cannot show that, so it says where it is shown instead.
        movements = [i for i in found if i.kind in CHANGE_KINDS]
        if movements:
            st.info(
                f"No strict price changed across {points} capture times. "
                f"{len(movements)} other movement"
                f"{'' if len(movements) == 1 else 's'} were observed in the "
                f"same window and are listed in section 2.")
        else:
            st.info(f"No price change observed across {points} capture times.")

# ------------------------------------------------------------- 2. findings
st.header("2. What the observations say")
st.caption("Findings are detected and scored deterministically. The model, when "
           "one is configured, chooses which of them to lead with. It never "
           "produces a figure, because it never computes one.")

if not found:
    st.info("No findings for the current selection.")
else:
    if st.button("Rank with the language model", disabled=settings is None,
                 key="rank_button"):
        with st.spinner("Asking the model which findings to lead with"):
            try:
                st.session_state["ranking"] = select_with_model(
                    found, build_client(settings), settings.model)
            except Exception as exc:  # noqa: BLE001
                st.session_state.pop("ranking", None)
                st.error(f"The endpoint did not answer: {exc}")
    if settings is None:
        st.caption(missing_reason)

    ranking = st.session_state.get("ranking") or select_by_score(found, 3)
    if ranking.framing:
        st.markdown(usd(f"**{ranking.framing}**"))
    for item in ranking.insights:
        st.markdown(usd(f"- {item.sentence}"))
    led = [g for g in lead if g in KIND_GROUPS]
    order = ("by score" if len(led) in (0, len(KIND_GROUPS))
             else f"by score, with {' and '.join(led)} read first")
    st.caption("Selected by the language model from the scored candidates."
               if ranking.source == "model"
               else f"Selected {order}. No model was involved.")

    with st.expander(f"All {len(found)} findings, and how each was ranked"):
        st.caption("priority = magnitude x recency x scope x rarity. It orders "
                   "the findings and means nothing else; it is not statistical "
                   "significance. The factors are shown so the order can be "
                   "argued with rather than trusted.")
        st.dataframe(pd.DataFrame([{
            "priority": round(item.significance, 3),
            "magnitude": round(item.facts.get("magnitude", 0.0), 2),
            "recency": round(item.facts.get("recency", 0.0), 2),
            "scope": round(item.facts.get("scope_weight", 0.0), 2),
            "rarity": round(item.facts.get("rarity", 0.0), 2),
            "kind": item.kind,
            "finding": item.sentence,
        } for item in found]), width="stretch", hide_index=True)


# ------------------------------------------------------------- 3. summary
st.header("3. Observation summary")

# The summary renders above the controls, but the controls have to run first so
# a click is reflected in the same run. A container reserves the space.
summary_area = st.container()

context = build_context(view, data.products)
stored = read_stored_summary()
if st.button("Regenerate with the language model", disabled=settings is None):
    with st.spinner("Asking the model, then looking up every figure it returns"):
        try:
            st.session_state["summary_run"] = (
                *summarise(context, build_client(settings), settings.model),
                settings.model)
        # Any endpoint failure is the endpoint's problem, not the page's: the
        # computed summary below is unaffected and the app stays up.
        except Exception as exc:  # noqa: BLE001
            st.session_state.pop("summary_run", None)
            st.error(f"The endpoint did not answer: {exc}")

if settings is None:
    st.caption(missing_reason)

run = st.session_state.get("summary_run")

if run and run[2] == "model" and st.button("Save as the stored summary"):
    write_stored_summary(run[0], run[3], context)
    st.success("Written to `data/summary.md`. That file is what this page shows "
               "by default from now on.")

with summary_area:
    if run and run[2] == "model":
        st.markdown(usd(run[0]))
        st.caption("Written by the language model in this session. Every figure "
                   "in it was found among the computed values before it was "
                   "displayed. Not stored yet.")
    elif run:
        st.warning(
            f"The model summary was discarded in full. These figures are not "
            f"in the computed values: {', '.join(run[1])}. A summary is never "
            f"shown with a figure that was not verified, so the computed "
            f"summary is below instead.")
        st.markdown(usd(template_summary(context)))
        st.caption("Computed directly from the recorded observations. No model "
                   "output is used here.")
    elif stored is not None and (filtered or stored_is_stale(stored, context)):
        # A stored summary describes the whole table as it stood when it was
        # written. Under a selection, or after a newer capture, it describes
        # something the reader is not looking at. Showing it first and
        # qualifying it afterwards means the wrong figures are read first, so
        # it is replaced rather than annotated.
        reason = ("the current selection" if filtered
                  else f"observations up to {stored.last_capture}, and the "
                       f"newest snapshot is from {context['last_capture']}")
        st.warning(f"The stored summary in `data/summary.md` does not describe "
                   f"{reason}. The computed summary for what is on screen is "
                   f"shown instead.")
        st.markdown(usd(template_summary(context)))
        st.caption("Computed directly from the recorded observations. No model "
                   "output is used here.")
    elif stored is not None:
        st.markdown(usd(stored.prose))
        st.caption(f"Written by `{stored.model}` on {stored.generated_at} and "
                   f"stored in `data/summary.md`. Every figure in it was found "
                   f"among the computed values before it was stored; that check "
                   f"bounds invention, not whether a figure is attached to the "
                   f"right product.")
    else:
        st.markdown(usd(template_summary(context)))
        st.caption("Computed directly from the recorded observations by string "
                   "formatting. No language model was called, and none is "
                   "needed to render this page.")

# ------------------------------------------------------------ 3. snapshot
st.header("4. Latest observation per product")

# Left join so a selected product with no usable snapshot stays visible instead
# of disappearing from the comparison. Joining from the whole master instead
# would put every unselected product back on the page as a row of blanks, which
# reads as missing data rather than as an excluded product.
table = data.products[data.products["sku"].isin(picked)].merge(
    latest, on="sku", how="left")
st.dataframe(
    table[["role", "brand", "model_name", "cpu", "form_factor", "display_type",
           "brightness_nits", "price", "regular_price", "savings",
           "availability", "seller", "captured_at"]],
    width="stretch", hide_index=True)

# ----------------------------------------------------------- 4. comparison
st.header("5. Strict group — matched-pair observation")

if not comparison["comparable"]:
    st.info(f"Not comparable: {comparison['reason']}.")
elif comparison["parity"]:
    # The figure is in the headline row. What is left here is the part that
    # needs the space: which fields the rule used, and which it did not.
    st.write(
        f"**Price parity at {comparison['captured_at']:%Y-%m-%d %H:%M}.** "
        "Both SKUs match on every field the equivalence rule uses — processor "
        "model, memory, storage, screen size, operating system, device type "
        "and form factor — and were listed at the same price. They still "
        "differ outside that key: panel brightness is 400 nits against 300.")
else:
    st.write(usd(
        f"At {comparison['captured_at']:%Y-%m-%d %H:%M}, "
        f"**{names[comparison['cheaper_sku']]} is "
        f"${comparison['delta']:.2f} below "
        f"{names[comparison['dearer_sku']]}**. This is a matched-pair "
        "observation at one moment; the difference is not attributed to any "
        "single attribute."))

# ------------------------------------------------------------ 5. reference
st.header("6. Reference products")
st.caption("Excluded from the chart and the comparison above. Each differs "
           "from the strict pair on more than one field, so no difference is "
           "attributed to any single attribute.")

reference = table[table["role"] == "reference"]
for product in reference.itertuples():
    price = "not observed" if pd.isna(product.price) else f"${product.price:,.2f}"
    note = ""
    if not pd.isna(product.savings) and product.savings:
        note = (f" — discounted ${product.savings:,.2f} from "
                f"${product.regular_price:,.2f}")
    st.write(usd(f"- **{product.brand} {product.model_name}** — {price}{note} · "
                 f"{product.cpu} · {product.form_factor} · "
                 f"{product.display_type} · {product.availability}"))

st.divider()
st.caption(usd(
    "Every product here is listed at $1,299.99 except the Intel variant at "
    "$1,349.99. Where a current price sits below list, that is promotional "
    "state."))
