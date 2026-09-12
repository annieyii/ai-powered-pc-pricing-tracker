"""Streamlit dashboard.

The only module that imports streamlit. There is deliberately no caching:
the documented workflow is to append a row to the CSV and reload, and
st.cache_data keys on the arguments rather than the file contents, so a cached
loader would keep serving the old file.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tracker.metrics import (
    latest_per_sku,
    series_for,
    strict_comparison,
    strict_time_points,
)
from tracker.store import build

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS_CSV = ROOT / "data" / "structured" / "products.csv"
PRICES_CSV = ROOT / "data" / "structured" / "prices_manual.csv"

# Two identical series must stay separable without moving either line off its
# real value, so they differ by dash pattern and marker instead.
DASHES = ["solid", "dash", "dot", "dashdot"]
MARKERS = ["circle", "square", "diamond", "x"]

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
                     use_container_width=True, hide_index=True)

st.caption(f"{data.accepted} of {data.total} rows accepted")

if data.prices.empty:
    st.warning("No usable snapshots yet. Append rows to "
               "`data/structured/prices_manual.csv` and reload this page.")
    st.stop()

latest = latest_per_sku(data.prices)
points = strict_time_points(data.prices, data.products)
st.caption(f"Last observation {data.prices['captured_at'].max():%Y-%m-%d %H:%M} · "
           f"{len(data.prices)} snapshots · "
           f"{points} capture time(s) covering the strict group")

# --------------------------------------------------------------- 1. trend
st.header("1. Price over time — strict equivalence group")

strict = data.products[data.products["role"] == "strict"]
figure = go.Figure()
for index, product in enumerate(strict.itertuples()):
    line = series_for(data.prices, product.sku)
    if line.empty:
        continue
    name = f"{product.brand} {product.model_name}"
    figure.add_trace(go.Scatter(
        x=line["captured_at"], y=line["price"], mode="lines+markers", name=name,
        line=dict(dash=DASHES[index % len(DASHES)], width=2),
        marker=dict(symbol=MARKERS[index % len(MARKERS)], size=11),
        hovertemplate="%{x|%b %d %H:%M}<br>$%{y:.2f}<extra>" + name + "</extra>",
    ))
figure.update_layout(yaxis_title="Price (USD)",
                     xaxis_title="Captured at (Asia/Taipei)",
                     hovermode="x unified", height=420,
                     legend=dict(orientation="h", y=-0.25))
st.plotly_chart(figure, use_container_width=True)

if points < 2:
    st.info(f"Only {points} capture time recorded so far. A trend needs at "
            "least two.")
else:
    moved = strict.merge(data.prices, on="sku").groupby("sku")["price"].nunique()
    if (moved <= 1).all():
        st.info(f"No price change observed across {points} capture times.")

# ------------------------------------------------------------ 2. snapshot
st.header("2. Latest observation per product")

# Left join from products so a product with no usable snapshot stays visible
# instead of disappearing from the comparison.
table = data.products.merge(latest, on="sku", how="left")
st.dataframe(
    table[["role", "brand", "model_name", "cpu", "form_factor", "display_type",
           "brightness_nits", "price", "regular_price", "savings",
           "availability", "seller", "captured_at"]],
    use_container_width=True, hide_index=True)

# ----------------------------------------------------------- 3. comparison
st.header("3. Strict group — matched-pair observation")

comparison = strict_comparison(data.prices, data.products)
names = dict(zip(data.products["sku"],
                 data.products["brand"] + " " + data.products["model_name"]))

if not comparison["comparable"]:
    st.info(f"Not comparable: {comparison['reason']}.")
elif comparison["parity"]:
    st.metric("Price difference", "$0.00")
    st.write(
        f"**Price parity at {comparison['captured_at']:%Y-%m-%d %H:%M}.** "
        "Both SKUs match on every field the equivalence rule uses — processor "
        "model, memory, storage, screen size, operating system, device type "
        "and form factor — and were listed at the same price. They still "
        "differ outside that key: panel brightness is 400 nits against 300.")
else:
    st.metric("Price difference", f"${comparison['delta']:.2f}")
    st.write(
        f"At {comparison['captured_at']:%Y-%m-%d %H:%M}, "
        f"**{names[comparison['cheaper_sku']]} is "
        f"${comparison['delta']:.2f} below "
        f"{names[comparison['dearer_sku']]}**. This is a matched-pair "
        "observation at one moment; the difference is not attributed to any "
        "single attribute.")

# ------------------------------------------------------------ 4. reference
st.header("4. Reference products")
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
    st.write(f"- **{product.brand} {product.model_name}** — {price}{note} · "
             f"{product.cpu} · {product.form_factor} · {product.display_type} "
             f"· {product.availability}")

st.divider()
st.caption("Every product here is listed at $1,299.99 except the Intel "
           "variant at $1,349.99. Where a current price sits below list, that "
           "is promotional state.")
