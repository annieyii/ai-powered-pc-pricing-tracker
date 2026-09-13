"""What is worth saying about the price tables, and how much it matters.

The pipeline this sits in separates deciding *what* to say from deciding *how*
to say it. Stage one is entirely deterministic: ``detect`` walks the observed
rows and emits an ``Insight`` for every signal it finds, ``score`` ranks them
from four documented factors, and one template function per kind turns the
facts into a sentence. No model is involved, so no figure in this module can be
anything other than computed.

Stage two is the only place a model appears, and its job is deliberately small:
it is handed the finished candidates and returns indices. It selects, it never
computes. A model that cannot emit a number cannot invent one, which is why the
ranked list is built before the model is called rather than by it.

``no_change`` is a first-class kind, not a fallback for an empty list. A report
that goes quiet when nothing moved is indistinguishable from a report that was
not run, and a reader fills that silence in with movement. The absence of
change is an observation and is stated as one.

Nothing here claims causation. ``promotion_active`` reports that a listed price
sits below a stated regular price at one moment; it does not offer that as the
reason for any difference between two products.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from tracker.metrics import STRICT, _strict_skus
from tracker.store import TIMESTAMP_FORMAT
# The endpoint configuration and the grounding rule already exist. A second
# copy of either would be a second thing to keep true.
from tracker.summarise import (  # noqa: F401  (re-exported for callers)
    MissingSetting,
    build_client,
    read_settings,
    verify_grounded,
)

ADD_TO_CART = "Add to cart"

#: Kinds that describe a movement, as opposed to a state or its absence.
CHANGE_KINDS = frozenset({
    "price_change", "parity_broken", "gap_widened", "gap_narrowed",
    "availability_lost", "stock_shift",
})


# --- scope --------------------------------------------------------------

@dataclass(frozen=True)
class Scope:
    """The slice of the observations a detection run is restricted to.

    ``None`` on any field means unrestricted, so the default ``Scope()`` is the
    whole table and a caller never has to special-case "no filter".
    """

    skus: tuple[str, ...] | None = None
    start: datetime | None = None
    end: datetime | None = None

    def key(self) -> str:
        """A stable string identifying this scope.

        Ranked results are expensive enough to cache and meaningless if shown
        against a different selection, so a caller compares this key to decide
        whether what it is holding still belongs to what the user is looking
        at. SKUs are sorted because the selection is a set: two orderings of
        the same three SKUs are the same scope and must not look like a change.
        """
        skus = "all" if self.skus is None else ",".join(sorted(self.skus))
        return (f"skus={skus}"
                f"|start={_stamp(self.start) or 'none'}"
                f"|end={_stamp(self.end) or 'none'}")

    def apply(self, prices: pd.DataFrame) -> pd.DataFrame:
        """The rows this scope admits, in the frame's own shape."""
        if prices.empty:
            return prices.copy()
        rows = prices
        if self.skus is not None:
            rows = rows[rows["sku"].astype(str).isin(set(self.skus))]
        if self.start is not None:
            rows = rows[rows["captured_at"] >= pd.Timestamp(self.start)]
        if self.end is not None:
            rows = rows[rows["captured_at"] <= pd.Timestamp(self.end)]
        return rows.reset_index(drop=True)


# --- the unit of output -------------------------------------------------

@dataclass(frozen=True)
class Insight:
    """One thing the data says, with the figures behind it and its rank.

    ``sentence`` is produced by the template for ``kind`` and by nothing else.
    Every figure in it is read out of ``facts``, and ``facts`` also carries the
    four scoring factors, so the ranking can be inspected rather than trusted.
    """

    kind: str
    subjects: tuple[str, ...]
    at: datetime | None
    facts: dict[str, Any] = field(default_factory=dict)
    significance: float = 0.0
    sentence: str = ""


# --- scoring: four pure factors, each documented ------------------------

#: A change worth this fraction of the list price saturates ``magnitude``.
MAGNITUDE_SATURATION = 0.10

#: What a kind carrying no figure is worth before the other factors apply.
BASE_MAGNITUDE = 0.20

#: What the earliest observation in the window retains after decay.
RECENCY_FLOOR = 0.30

#: How much each kind adds by being unusual. A movement outranks a state, and
#: a state outranks the absence of movement. Parity coming apart is the one
#: parity result a reader has to act on, so it outranks parity holding.
RARITY = {
    "price_change": 1.00,
    "availability_lost": 0.90,
    "parity_broken": 0.85,
    "gap_widened": 0.75,
    "gap_narrowed": 0.75,
    "stock_shift": 0.65,
    "promotion_ending": 0.60,
    "promotion_active": 0.45,
    "stock_scarcity": 0.35,
    "parity_held": 0.30,
    "no_change": 0.20,
}


def magnitude(change: float | None, reference_price: float | None) -> float:
    """How large a movement is, as a fraction of what the thing costs.

    Absolute dollars do not compare across price points, so the change is
    divided by the list price. It is then divided again by
    ``MAGNITUDE_SATURATION``, which sets the point at which a movement is
    simply "large": a ten percent move scores 1.0 and anything bigger is
    clipped there, because the difference between a 30 percent cut and a 40
    percent cut does not change what a reader does about it.

    A kind that carries no figure gets ``BASE_MAGNITUDE``. It is deliberately
    low, so that a state such as a stock note cannot outrank a real move, and
    deliberately non-zero, so that it cannot be ranked out of existence.
    """
    if change is None or not reference_price:
        return BASE_MAGNITUDE
    share = abs(float(change)) / (float(reference_price) * MAGNITUDE_SATURATION)
    return min(1.0, share)


def recency(at: Any, earliest: Any, latest: Any) -> float:
    """How much the observation's position in the window is worth.

    Linear from ``RECENCY_FLOOR`` at the earliest observation to 1.0 at the
    latest. The floor exists because an old observation is still an
    observation: decaying to zero would let a long window silently delete its
    own beginning, and a reader who chose that window asked to see it.

    A window with a single moment in it has no ordering to express, so
    everything in it scores 1.0.
    """
    if at is None or earliest is None or latest is None:
        return 1.0
    at, earliest, latest = (pd.Timestamp(at), pd.Timestamp(earliest),
                            pd.Timestamp(latest))
    span = (latest - earliest).total_seconds()
    if span <= 0:
        return 1.0
    position = (at - earliest).total_seconds() / span
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * min(1.0, max(0.0, position))


def scope_weight(roles: tuple[str, ...]) -> float:
    """1.0 for a strict product, 0.5 for a reference one.

    The strict pair is the comparison the tool exists to make. A reference
    product is context, so it is worth reporting and worth reporting second.
    An insight spanning several products takes the highest weight among them:
    a finding that touches the strict pair is a strict finding even when a
    reference product is named alongside.
    """
    if not roles:
        return 0.5
    return max(1.0 if role == STRICT else 0.5 for role in roles)


def rarity(kind: str) -> float:
    """How unusual this kind of finding is, from the ``RARITY`` table."""
    return RARITY.get(kind, 0.5)


def score(kind: str, roles: tuple[str, ...], at: Any = None,
          earliest: Any = None, latest: Any = None,
          change: float | None = None,
          reference_price: float | None = None) -> dict[str, float]:
    """The four factors and their product, in 0..1.

    Returned as a dictionary rather than a bare float so the caller can put the
    factors on ``Insight.facts``. A single number nobody can take apart is a
    number nobody can argue with, which is the wrong property for a ranking
    that decides what a person reads first.
    """
    parts = {
        "magnitude": round(magnitude(change, reference_price), 6),
        "recency": round(recency(at, earliest, latest), 6),
        "scope_weight": scope_weight(roles),
        "rarity": rarity(kind),
    }
    product = 1.0
    for value in parts.values():
        product *= value
    parts["significance"] = round(min(1.0, max(0.0, product)), 6)
    return parts


# --- rendering: one template per kind, plain string formatting ----------

def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


def _times(count: int) -> str:
    return f"{count} capture time" + ("" if count == 1 else "s")


def _render_price_change(f: dict[str, Any]) -> str:
    direction = "rose" if f["change"] > 0 else "fell"
    return (f"{f['name']} {direction} from {_money(f['previous_price'])} to "
            f"{_money(f['price'])}, a move of {_money(abs(f['change']))}, "
            f"between {f['previous_at']} and {f['at']}.")


def _render_parity_held(f: dict[str, Any]) -> str:
    return (f"Matched-pair observation: {f['names']} were listed at the same "
            f"price at all {_times(f['shared_times'])} they shared, most "
            f"recently {_money(f['price'])} at {f['at']}.")


def _render_parity_broken(f: dict[str, Any]) -> str:
    return (f"Matched-pair observation: {f['names']} were level at "
            f"{_money(f['previous_price'])} at {f['previous_at']} and "
            f"{_money(f['gap'])} apart at {f['at']}.")


def _render_gap_widened(f: dict[str, Any]) -> str:
    return (f"Matched-pair observation: the gap between {f['names']} widened "
            f"from {_money(f['previous_gap'])} to {_money(f['gap'])} between "
            f"{f['previous_at']} and {f['at']}.")


def _render_gap_narrowed(f: dict[str, Any]) -> str:
    return (f"Matched-pair observation: the gap between {f['names']} narrowed "
            f"from {_money(f['previous_gap'])} to {_money(f['gap'])} between "
            f"{f['previous_at']} and {f['at']}.")


def _render_promotion_active(f: dict[str, Any]) -> str:
    return (f"{f['name']} was listed at {_money(f['price'])} against a stated "
            f"regular price of {_money(f['regular_price'])}, a discount of "
            f"{_money(f['discount'])}, at {f['at']}.")


def _render_promotion_ending(f: dict[str, Any]) -> str:
    return (f"The page for {f['name']} stated the offer ends {f['ends']}, as "
            f"read at {f['at']}.")


def _render_stock_scarcity(f: dict[str, Any]) -> str:
    return (f"The page for {f['name']} carried the stock note "
            f"\"{f['stock_hint']}\" at {f['at']}.")


def _render_stock_shift(f: dict[str, Any]) -> str:
    was = f["previous_stock_hint"] or "no stock note"
    now = f["stock_hint"] or "no stock note"
    return (f"The stock note on {f['name']} changed from {was!r} to {now!r} "
            f"between {f['previous_at']} and {f['at']}.")


def _render_availability_lost(f: dict[str, Any]) -> str:
    return (f"{f['name']} moved from {f['previous_availability']} to "
            f"{f['availability']} between {f['previous_at']} and {f['at']}.")


def _render_no_change(f: dict[str, Any]) -> str:
    return (f"No strict price changed across the {_times(f['capture_times'])} "
            f"covering {f['names']}, from {f['first_at']} to {f['at']}.")


RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "price_change": _render_price_change,
    "parity_held": _render_parity_held,
    "parity_broken": _render_parity_broken,
    "gap_widened": _render_gap_widened,
    "gap_narrowed": _render_gap_narrowed,
    "promotion_active": _render_promotion_active,
    "promotion_ending": _render_promotion_ending,
    "stock_scarcity": _render_stock_scarcity,
    "stock_shift": _render_stock_shift,
    "availability_lost": _render_availability_lost,
    "no_change": _render_no_change,
}


def render(kind: str, facts: dict[str, Any]) -> str:
    """The sentence for one kind, formatted from its own facts and nothing else."""
    renderer = RENDERERS.get(kind)
    if renderer is None:
        raise KeyError(f"no template for insight kind {kind!r}")
    return renderer(facts)


# --- reading the frames -------------------------------------------------

def _stamp(value: Any) -> str | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return pd.Timestamp(value).strftime(TIMESTAMP_FORMAT)


def _value(record: dict[str, Any], key: str) -> Any:
    """One cell, with a blank, a missing column and a NaN all reading as None."""
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _number(record: dict[str, Any], key: str) -> float | None:
    value = _value(record, key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: A stated end date in the note field. The note is free text copied off the
#: page, so this looks for the word and a date beside it rather than trying to
#: parse the whole sentence. "ends soon" carries no date and does not fire.
_END_DATE = re.compile(
    r"\bends?\b\s*(?:on\s+)?"
    r"(\d{4}-\d{2}-\d{2}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,?\s+\d{2,4})?"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
    re.IGNORECASE)


def stated_end_date(note: Any) -> str | None:
    """The end date a note states, or None when it states none."""
    if not note:
        return None
    found = _END_DATE.search(str(note))
    return " ".join(found.group(1).split()) if found else None


def _names(products: pd.DataFrame) -> dict[str, str]:
    if products.empty or "brand" not in products.columns:
        return {}
    return {str(sku): f"{brand} {model}" for sku, brand, model
            in zip(products["sku"], products["brand"], products["model_name"])}


def _roles(products: pd.DataFrame) -> dict[str, str]:
    if products.empty or "role" not in products.columns:
        return {}
    return {str(sku): str(role) for sku, role
            in zip(products["sku"], products["role"])}


def _list_prices(products: pd.DataFrame) -> dict[str, float]:
    if products.empty or "list_price" not in products.columns:
        return {}
    out: dict[str, float] = {}
    for sku, price in zip(products["sku"], products["list_price"]):
        try:
            out[str(sku)] = float(price)
        except (TypeError, ValueError):
            continue
    return out


# --- detection ----------------------------------------------------------

def detect(prices: pd.DataFrame, products: pd.DataFrame,
           scope: Scope | None = None) -> list[Insight]:
    """Every signal the observations support, most significant first.

    Entirely deterministic. The same frames produce the same list in the same
    order on every run, which is what makes the ranking arguable and the stage
    below it replaceable.
    """
    rows = (scope or Scope()).apply(prices)
    if rows.empty:
        return []

    names = _names(products)
    roles = _roles(products)
    lists = _list_prices(products)
    strict = sorted(_strict_skus(products) & set(rows["sku"].astype(str)))

    earliest = rows["captured_at"].min()
    latest = rows["captured_at"].max()

    def name_of(sku: str) -> str:
        return names.get(sku, sku)

    def build(kind: str, subjects: tuple[str, ...], at: Any,
              facts: dict[str, Any], change: float | None = None,
              reference: float | None = None) -> Insight:
        parts = score(kind, tuple(roles.get(s, "reference") for s in subjects),
                      at=at, earliest=earliest, latest=latest,
                      change=change, reference_price=reference)
        full = dict(facts)
        full.update(parts)
        return Insight(kind=kind, subjects=subjects,
                       at=None if at is None else pd.Timestamp(at),
                       facts=full, significance=parts["significance"],
                       sentence=render(kind, full))

    found: list[Insight] = []
    strict_changed = False

    # --- per SKU: movement between one observation and the next ---------
    for sku, group in rows.groupby(rows["sku"].astype(str), sort=True):
        records = group.sort_values("captured_at").to_dict("records")
        reference = lists.get(sku) or _number(records[0], "price")

        for previous, current in zip(records, records[1:]):
            was, now = _number(previous, "price"), _number(current, "price")
            if was is not None and now is not None and was != now:
                if sku in strict:
                    strict_changed = True
                found.append(build(
                    "price_change", (sku,), current["captured_at"],
                    {"sku": sku, "name": name_of(sku), "price": now,
                     "previous_price": was, "change": round(now - was, 2),
                     "at": _stamp(current["captured_at"]),
                     "previous_at": _stamp(previous["captured_at"])},
                    change=now - was, reference=reference))

            # A standing stock note says what stock looks like now. A changed
            # one says something happened, which is the observation a
            # price-only page drops: a window can hold no price movement at
            # all and still hold plenty of movement.
            wasnote = str(_value(previous, "stock_hint") or "")
            nownote = str(_value(current, "stock_hint") or "")
            if wasnote != nownote:
                found.append(build(
                    "stock_shift", (sku,), current["captured_at"],
                    {"sku": sku, "name": name_of(sku), "stock_hint": nownote,
                     "previous_stock_hint": wasnote,
                     "at": _stamp(current["captured_at"]),
                     "previous_at": _stamp(previous["captured_at"])}))

            before = _value(previous, "availability")
            after = _value(current, "availability")
            if before == ADD_TO_CART and after is not None and after != ADD_TO_CART:
                found.append(build(
                    "availability_lost", (sku,), current["captured_at"],
                    {"sku": sku, "name": name_of(sku), "availability": after,
                     "previous_availability": before,
                     "at": _stamp(current["captured_at"]),
                     "previous_at": _stamp(previous["captured_at"])}))

        # --- per SKU: the state its most recent observation is in -------
        last = records[-1]
        at = last["captured_at"]
        price = _number(last, "price")
        regular = _number(last, "regular_price")
        if price is not None and regular is not None and regular > price:
            discount = round(regular - price, 2)
            found.append(build(
                "promotion_active", (sku,), at,
                {"sku": sku, "name": name_of(sku), "price": price,
                 "regular_price": regular, "discount": discount,
                 "at": _stamp(at)},
                change=discount, reference=reference))

        ends = stated_end_date(_value(last, "note"))
        if ends:
            found.append(build("promotion_ending", (sku,), at,
                               {"sku": sku, "name": name_of(sku),
                                "ends": ends, "at": _stamp(at)}))

        hint = _value(last, "stock_hint")
        if hint:
            found.append(build("stock_scarcity", (sku,), at,
                               {"sku": sku, "name": name_of(sku),
                                "stock_hint": str(hint), "at": _stamp(at)}))

    found.extend(_pair_insights(rows, strict, name_of, build))
    found.extend(_no_change(rows, strict, name_of, build, strict_changed))

    return sorted(found, key=lambda i: (-i.significance, i.kind, i.subjects))


def _shared_times(rows: pd.DataFrame, strict: list[str]) -> list[dict[str, Any]]:
    """Every moment at which all the strict SKUs carried a usable price.

    Two prices are only comparable if they were observed together. Pairing each
    SKU's own nearest observation would present two different moments as one,
    which is the whole thing ``metrics.strict_comparison`` exists to refuse, so
    the pair kinds walk shared moments only.
    """
    if len(strict) < 2:
        return []
    priced = rows[rows["sku"].astype(str).isin(strict) & rows["price"].notna()]
    shared: list[dict[str, Any]] = []
    for moment in sorted(priced["captured_at"].unique()):
        at_moment = priced[priced["captured_at"] == moment]
        if set(at_moment["sku"].astype(str)) != set(strict):
            continue
        values = [float(p) for p in at_moment["price"]]
        shared.append({"at": moment, "low": min(values), "high": max(values),
                       "gap": round(max(values) - min(values), 2)})
    return shared


def _pair_insights(rows, strict, name_of, build) -> list[Insight]:
    """Parity and gap findings over the strict pair's shared moments."""
    shared = _shared_times(rows, strict)
    if not shared:
        return []

    subjects = tuple(strict)
    label = " and ".join(name_of(sku) for sku in strict)
    reference = max(point["high"] for point in shared)
    out: list[Insight] = []

    if all(point["gap"] == 0.0 for point in shared):
        last = shared[-1]
        out.append(build("parity_held", subjects, last["at"],
                         {"skus": list(strict), "names": label,
                          "price": last["low"], "shared_times": len(shared),
                          "at": _stamp(last["at"])}))

    for previous, current in zip(shared, shared[1:]):
        common = {"skus": list(strict), "names": label,
                  "gap": current["gap"], "previous_gap": previous["gap"],
                  "previous_price": previous["low"],
                  "at": _stamp(current["at"]),
                  "previous_at": _stamp(previous["at"])}
        if previous["gap"] == 0.0 and current["gap"] > 0.0:
            out.append(build("parity_broken", subjects, current["at"], common))
        if current["gap"] > previous["gap"]:
            out.append(build("gap_widened", subjects, current["at"], dict(common),
                             change=current["gap"] - previous["gap"],
                             reference=reference))
        elif current["gap"] < previous["gap"]:
            out.append(build("gap_narrowed", subjects, current["at"], dict(common),
                             change=previous["gap"] - current["gap"],
                             reference=reference))
    return out


def _no_change(rows, strict, name_of, build, strict_changed) -> list[Insight]:
    """Stability over the window, stated rather than left to be inferred.

    This is not an empty-list fallback. It fires whenever the strict group was
    observed at two or more moments and none of those observations moved, and
    it fires alongside whatever else was found. A reader who is told nothing
    about movement concludes there was movement nobody wrote down.
    """
    if not strict or strict_changed:
        return []
    times = sorted(rows[rows["sku"].astype(str).isin(strict)]["captured_at"].unique())
    if len(times) < 2:
        return []
    return [build("no_change", tuple(strict), times[-1],
                  {"skus": list(strict),
                   "names": " and ".join(name_of(sku) for sku in strict),
                   "capture_times": len(times),
                   "first_at": _stamp(times[0]), "at": _stamp(times[-1])})]


# --- stage two: the model selects, and only selects ---------------------

SELECTION_PROMPT = (
    "You choose which already written findings a product manager reads first. "
    "The next message is a JSON list of candidates, each with an index, a "
    "kind, a significance score and the sentence itself.\n"
    "Rules you must follow:\n"
    "1. Return a single JSON object with the keys \"indices\" and \"framing\".\n"
    "2. \"indices\" is a list of at most {k} integers, each one an index that "
    "appears in the candidate list. Choose the findings that most warrant a "
    "human look.\n"
    "3. Do not write, rewrite, shorten or correct any sentence. You select "
    "them; you do not author them.\n"
    "4. \"framing\" is one short line of context, or null. State no figure in "
    "it that does not already appear in the candidates. Do not calculate, "
    "round or recall a number from anywhere else.\n"
    "5. Report only what the candidates observe. Do not say that one finding "
    "explains another, and do not forecast."
)


@dataclass(frozen=True)
class Selection:
    """What is to be shown, and who decided it.

    ``source`` is on the record because the two paths are not equally
    defensible. A reader who is looking at a model's choice is entitled to know
    that, and a reviewer who is looking at a fallback is entitled to know the
    model was asked and did not answer usefully.
    """

    insights: tuple[Insight, ...]
    framing: str | None
    source: str


def select_by_score(insights: list[Insight], k: int = 3) -> Selection:
    """The top k by significance. No client, no endpoint, no network.

    This is the floor the page always stands on, and the exact result any model
    failure lands back on, so it is written first and the model path is defined
    in terms of it.
    """
    return Selection(insights=tuple(insights[:max(0, k)]), framing=None,
                     source="score")


def _candidates(insights: list[Insight]) -> list[dict[str, Any]]:
    """The only thing the model is shown: index, kind, score, sentence."""
    return [{"index": i, "kind": insight.kind,
             "significance": insight.significance, "sentence": insight.sentence}
            for i, insight in enumerate(insights)]


def _parse_reply(content: str) -> dict[str, Any] | None:
    """The JSON object in a reply, tolerating a code fence around it."""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _chosen(reply: dict[str, Any], count: int, k: int) -> list[int] | None:
    """The indices, or None if any one of them was not on offer.

    One bad index invalidates the whole reply rather than being dropped from
    it. An index out of range means the model was not reading the list it was
    given, and the remaining indices come with no more reason to be trusted
    than that one did.
    """
    raw = reply.get("indices")
    if not isinstance(raw, list) or not raw or len(raw) > k:
        return None
    chosen: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not 0 <= value < count or value in chosen:
            return None
        chosen.append(value)
    return chosen


def select_with_model(insights: list[Insight], client: Any, model: str,
                      k: int = 3) -> Selection:
    """Ask the model which findings to lead with, and verify what comes back.

    The model receives finished sentences and returns indices. It is never in a
    position to state a figure, because it is never asked for one, so the
    familiar failure of a fabricated price cannot occur on this path at all.

    The one piece of prose it may write is the framing line, and that goes
    through the same grounding check the summary uses. A framing line holding a
    figure the candidates never contained is dropped on its own: the selection
    was made from a validated list and is still good, so discarding it too
    would throw away sound work to punish an unsound sentence.
    """
    if not insights:
        return select_by_score(insights, k)

    payload = _candidates(insights)
    try:
        reply = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SELECTION_PROMPT.format(k=k)},
                {"role": "user", "content": json.dumps(payload, indent=2,
                                                       default=str)},
            ],
            temperature=0)
        parsed = _parse_reply(reply.choices[0].message.content or "")
    except Exception:
        parsed = None

    if parsed is None:
        return select_by_score(insights, k)

    chosen = _chosen(parsed, len(insights), k)
    if chosen is None:
        return select_by_score(insights, k)

    framing = parsed.get("framing")
    framing = framing.strip() if isinstance(framing, str) else None
    if framing and verify_grounded(framing, {"candidates": payload}):
        framing = None

    return Selection(insights=tuple(insights[i] for i in chosen),
                     framing=framing or None, source="model")
