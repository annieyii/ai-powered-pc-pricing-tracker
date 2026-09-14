"""Natural-language observation summary, grounded in the computed values.

Acquisition ambiguity ends the moment an official structured feed exists: an
API that returns a price does not need a model to read one. Interpretation
ambiguity does not end, and it grows with the number of rows. So the
product-facing place for a language model in this tool is analysis, not
parsing, and this module is that layer.

The model is given no rows and no page text. It receives ``build_context``,
which is a dictionary of values ``metrics.py`` has already computed, and it is
told it may state no figure that is not in there. What comes back is checked
digit by digit against that same dictionary, and a summary carrying a figure
the context does not contain is thrown away in full rather than edited down.
There is no partial trust: a note that invents one number has demonstrated it
will invent another, and the deterministic ``template_summary`` is always
available to take its place.

``template_summary`` is therefore the floor, not the fallback of last resort.
It needs no endpoint, no key and no network, and it is what the dashboard
renders when nothing else is present.

Run as a script, this module writes a verified summary to ``data/summary.md``
so a reader with no endpoint of their own still sees real model-written
prose. The dashboard reads that file; it never writes it without an explicit
action, and it never calls an endpoint on page load.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# The endpoint configuration and the grounding rule are extraction's, reused
# rather than restated. A second copy of either would be a second thing to keep
# true.
from tracker.extract import (
    PRODUCTS_CSV,
    ROOT,
    MissingSetting,
    Settings,
    _digits,
    build_client,
    read_settings,
)
from tracker.metrics import (
    _strict_skus,
    latest_per_sku,
    strict_comparison,
    strict_time_points,
)
from tracker.store import TIMESTAMP_FORMAT, Dataset

PRICES_CSV = ROOT / "data" / "structured" / "prices_manual.csv"
SUMMARY_MD = ROOT / "data" / "summary.md"

HEADER = "<!-- model={model} generated={generated} last_capture={last_capture} -->"

SYSTEM_PROMPT = (
    "You write a short competitive pricing note for a product manager. Your "
    "only source is the JSON object of already computed observations in the "
    "next message.\n"
    "Rules you must follow:\n"
    "1. State no figure that does not appear in that JSON. Do not calculate, "
    "round, convert or recall a number from anywhere else.\n"
    "2. Do not claim any price moved unless the JSON says a price changed. If "
    "nothing changed, say plainly that nothing changed.\n"
    "3. Treat the two strict products as a matched-pair observation at one "
    "moment. Do not attribute a price difference to any single attribute.\n"
    "4. Report only what is observed. No forecast, no advice on what to "
    "charge.\n"
    "Write at most 150 words of plain sentences, then a final short line "
    "naming the one or two things that warrant a human look."
)


@dataclass(frozen=True)
class StoredSummary:
    """A summary previously generated, verified and written to disk."""

    prose: str
    model: str
    generated_at: str
    last_capture: str


# --- configuration, reported rather than raised -------------------------

def settings_or_reason(env: Mapping[str, str]) -> tuple[Settings | None, str | None]:
    """The endpoint configuration, or the reason there is none.

    The dashboard has to render with nothing configured, so a missing variable
    disables a button and names itself in a caption. It is the same refusal to
    guess an endpoint that ``extract.read_settings`` makes, with the exception
    turned into a value because a page cannot handle one.
    """
    try:
        return read_settings(env), None
    except MissingSetting as exc:
        return None, str(exc)


# --- the facts the model is allowed to use ------------------------------

def _money(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 2)


def _stamp(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime(TIMESTAMP_FORMAT)


def build_context(dataset: Dataset, products: pd.DataFrame) -> dict[str, Any]:
    """Every value the summary is permitted to mention, and nothing else.

    Deterministic, and assembled only from figures the metrics functions have
    already produced. No raw row and no free text reaches it, so the model
    cannot read a price off a page and cannot repeat a seller's own wording.
    This dictionary is also the whitelist ``verify_grounded`` checks against,
    which is why it is built once and used for both jobs.

    Products with no usable snapshot stay in the list with null prices, the
    same left join the table on the page uses. Dropping them would let the
    summary describe a shorter product set than the dashboard shows.
    """
    prices = dataset.prices
    table = products.merge(latest_per_sku(prices), on="sku", how="left")
    names = dict(zip(products["sku"], products["brand"] + " " + products["model_name"]))

    rows = [{
        "sku": str(row.sku),
        "name": f"{row.brand} {row.model_name}",
        "role": row.role,
        "price": _money(row.price),
        "regular_price": _money(row.regular_price),
        "savings": _money(row.savings),
        "availability": None if pd.isna(row.availability) else str(row.availability),
        "captured_at": _stamp(row.captured_at),
    } for row in table.itertuples()]

    comparison = strict_comparison(prices, products)
    strict_prices = prices[prices["sku"].isin(_strict_skus(products))]
    changed = (not strict_prices.empty
               and bool(strict_prices.groupby("sku")["price"].nunique().gt(1).any()))

    return {
        "capture_times": 0 if prices.empty else int(prices["captured_at"].nunique()),
        "strict_capture_times": strict_time_points(prices, products),
        "first_capture": None if prices.empty else _stamp(prices["captured_at"].min()),
        "last_capture": None if prices.empty else _stamp(prices["captured_at"].max()),
        "products": rows,
        "strict_comparison": {
            "comparable": bool(comparison["comparable"]),
            "captured_at": _stamp(comparison["captured_at"]),
            "delta": _money(comparison["delta"]),
            "cheaper": names.get(comparison["cheaper_sku"]),
            "dearer": names.get(comparison["dearer_sku"]),
            "parity": comparison["parity"],
            "reason": comparison.get("reason"),
        },
        "any_strict_price_changed": changed,
    }


# --- the summary that always works --------------------------------------

def _price_line(row: Mapping[str, Any]) -> str:
    if row["price"] is None:
        return f"- {row['name']} ({row['role']}): no usable price recorded"
    parts = [f"${row['price']:,.2f}"]
    if row["savings"] and row["regular_price"]:
        parts.append(f"discounted ${row['savings']:,.2f} from "
                     f"${row['regular_price']:,.2f}")
    if row["availability"]:
        parts.append(str(row["availability"]))
    return f"- {row['name']} ({row['role']}): {', '.join(parts)}"


def _pair_line(comparison: Mapping[str, Any]) -> str:
    if not comparison["comparable"]:
        return f"The strict pair is not comparable: {comparison['reason']}."
    if comparison["parity"]:
        return (f"Matched-pair observation at {comparison['captured_at']}: both "
                f"strict products were listed at the same price.")
    return (f"Matched-pair observation at {comparison['captured_at']}: "
            f"{comparison['cheaper']} was listed ${comparison['delta']:,.2f} "
            f"below {comparison['dearer']}. The difference is not attributed "
            f"to any single attribute.")


def _movement_line(context: Mapping[str, Any]) -> str:
    points = context["strict_capture_times"]
    if context["any_strict_price_changed"]:
        return (f"At least one strict price changed across the {points} capture "
                f"times covering the strict group.")
    if points < 2:
        return ("Only one capture time covers the strict group, so no movement "
                "can be reported yet.")
    return (f"No strict price changed across the {points} capture times "
            f"covering the strict group.")


def template_summary(context: Mapping[str, Any]) -> str:
    """The same observations in prose, built by string formatting alone.

    This is the floor the page can always stand on: no endpoint, no key, no
    network, no way to state a figure that was not computed. It is what renders
    by default, and what a discarded model summary is replaced with.
    """
    if not context["capture_times"] or not context["products"]:
        return ("No price observations have been recorded yet, so there is "
                "nothing to summarise.")

    if context["capture_times"] == 1:
        window = f"A single capture time at {context['last_capture']}."
    else:
        window = (f"{context['capture_times']} capture times from "
                  f"{context['first_capture']} to {context['last_capture']}.")

    lines = [window, "", "Latest listed price per product:"]
    lines += [_price_line(row) for row in context["products"]]
    lines += ["", _pair_line(context["strict_comparison"]),
              _movement_line(context)]
    return "\n".join(lines)


# --- grounding: the same rule extraction applies to a spec ---------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _canonical(token: str) -> str:
    """Strip the notation from a number so only its value is compared."""
    text = token.replace(",", "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _context_numbers(context: Mapping[str, Any]) -> set[str]:
    """Every numeric token the computed values offer.

    ``_digits`` is extraction's rule, imported unchanged: digit runs, so a
    timestamp grounds the hour written on its own. Prose needs one addition on
    this side, because it writes money as ``$1,299.99`` where the computed
    value is ``1299.99``; digit runs alone would split that into 1, 299 and 99
    and report findings against a figure that is exactly right.
    """
    tokens: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif value is not None and not isinstance(value, bool):
            tokens.update(_digits(value))
            tokens.update(_canonical(m) for m in _NUMBER.findall(str(value)))

    walk(context)
    return tokens


def verify_grounded(prose: str, context: Mapping[str, Any]) -> list[str]:
    """Numbers in the prose that the computed values never contained.

    This is the extraction module's anti-hallucination check pointed at prose
    instead of a specification: a figure the context never held was not read,
    it was invented, and a range check would not catch it because an invented
    price is a perfectly plausible price. Reported as written, so the page can
    name the value it refused.

    **What it does not check.** Membership of numeric tokens, and nothing else.
    Everything below passes:

    * a figure attached to the wrong subject, because the figure is present:
      `Dell 14S was listed at $1,299.99` is wrong and grounded, as is a count
      borrowed from an unrelated field;
    * a number carrying the wrong unit, since only the digits are compared;
    * any sentence with no figure in it at all, including a causal claim, a
      forecast or a recommendation. The system prompt forbids all three and
      nothing here enforces that.

    Catching any of these needs the claim parsed and matched to its subject,
    which this does not attempt. The check bounds invention, not
    interpretation, and the summary is worth exactly that much. A reviewer
    weighing how far to trust the prose should read this paragraph as the
    limit, not the docstring above it.
    """
    grounded = _context_numbers(context)
    seen: set[str] = set()
    ungrounded: list[str] = []
    for token in _NUMBER.findall(prose):
        value = _canonical(token)
        if value not in grounded and value not in seen:
            seen.add(value)
            ungrounded.append(token)
    return ungrounded


# --- generation, verified before it is ever shown -----------------------

def generate_summary(context: Mapping[str, Any], client: Any, model: str,
                     correction: str | None = None) -> str:
    """One chat completion over the context, as JSON and nothing else."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, indent=2, default=str)},
    ]
    if correction:
        messages.append({"role": "user", "content": correction})

    reply = client.chat.completions.create(
        model=model, messages=messages, temperature=0)
    return (reply.choices[0].message.content or "").strip()


def summarise(context: Mapping[str, Any], client: Any,
              model: str) -> tuple[str, list[str], str]:
    """Model prose if every figure in it checks out, the template otherwise.

    An ungrounded figure discards the whole summary rather than the sentence
    holding it. The reader cannot tell which sentences were invented, so a
    partially trusted note is worth less than a deterministic one.

    One retry, told exactly which figures were refused, and then the template.
    A model that invents twice is not going to stop on the fifth attempt, and
    an unbounded loop turns a bad endpoint into a large bill.
    """
    correction: str | None = None
    ungrounded: list[str] = []

    for _ in range(2):
        prose = generate_summary(context, client, model, correction)
        ungrounded = verify_grounded(prose, context)
        if not ungrounded:
            return prose, [], "model"
        correction = (
            "These figures do not appear in the observations you were given: "
            f"{', '.join(ungrounded)}. Rewrite the note using only figures "
            "present in that JSON, or leave the figure out of the sentence.")

    return template_summary(context), ungrounded, "template"


# --- the stored summary the dashboard reads -----------------------------

def read_stored_summary(path: Path | str | None = None) -> StoredSummary | None:
    """The committed summary, or None if none has been generated.

    The default is resolved on the call rather than bound to the signature, so
    the location stays a single module-level fact.
    """
    path = Path(path or SUMMARY_MD)
    if not path.is_file():
        return None

    text = path.read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    if first.startswith("<!--"):
        fields = dict(re.findall(r"(\w+)=(\S+)", first))
        prose = body.strip()
    else:
        fields, prose = {}, text.strip()

    return StoredSummary(prose=prose,
                         model=fields.get("model", "unrecorded"),
                         generated_at=fields.get("generated", "unrecorded"),
                         last_capture=fields.get("last_capture", ""))


def write_stored_summary(prose: str, model: str, context: Mapping[str, Any],
                         path: Path | str | None = None,
                         now: datetime | None = None) -> Path:
    """Write verified prose with the model, the time and the data it saw.

    The last capture time goes in the header so the file can later be caught
    describing observations that have since been overtaken. A summary with no
    record of what it was generated from cannot be told apart from a current
    one, and stale prose presented as current is worse than no prose.

    Only ever called with output that passed ``verify_grounded``.
    """
    path = Path(path or SUMMARY_MD)
    header = HEADER.format(model=model,
                           generated=(now or datetime.now()).strftime(TIMESTAMP_FORMAT),
                           last_capture=context.get("last_capture") or "none")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header}\n\n{prose.strip()}\n", encoding="utf-8")
    return path


def stored_is_stale(stored: StoredSummary, context: Mapping[str, Any]) -> bool:
    """True when snapshots have been recorded since the summary was written.

    **Newer only.** The header records the latest capture the summary saw, so
    a correction to an existing row or a deleted row leaves that timestamp
    where it was and this returns False: the stored prose can outlive the
    figures it describes. Catching that needs the summary to carry a digest of
    what it was generated from rather than one timestamp, which this does not
    do. The page's other guard covers the common case, since any active filter
    replaces the stored summary outright.
    """
    latest = context.get("last_capture")
    if not latest or not stored.last_capture:
        return False
    return stored.last_capture < latest


def main() -> int:
    """Generate the stored summary. Run by hand, never by the dashboard."""
    from tracker.store import build

    settings, reason = settings_or_reason(os.environ)
    if settings is None:
        print(reason, file=sys.stderr)
        return 2

    data = build(PRODUCTS_CSV, PRICES_CSV)
    context = build_context(data, data.products)
    prose, ungrounded, source = summarise(context, build_client(settings),
                                          settings.model)

    if source != "model":
        print("the model summary was discarded after one retry; these figures "
              f"are not in the computed values: {', '.join(ungrounded)}. "
              "Nothing was written.", file=sys.stderr)
        return 1

    path = write_stored_summary(prose, settings.model, context)
    print(f"verified summary written to {path.relative_to(ROOT)}, generated "
          f"from observations up to {context['last_capture']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
