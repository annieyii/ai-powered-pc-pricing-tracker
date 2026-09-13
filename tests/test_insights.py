"""Detection, ranking, rendering and the model's one job.

Stage one takes DataFrames only: no database, no files, no streamlit. Stage two
takes a stub with the same ``chat.completions.create(...)`` shape the SDK
exposes, so what the model is supposed to have returned is something each test
states rather than discovers. No test reaches the network.
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from tracker.insights import (
    KIND_GROUPS,
    prefer,
    RENDERERS,
    Insight,
    Scope,
    detect,
    magnitude,
    recency,
    render,
    scope_weight,
    score,
    select_by_score,
    select_with_model,
    stated_end_date,
)

LENOVO, HP, DELL = "6672159", "6668002", "6679150"

T1, T2, T3 = "2026-09-12T15:07", "2026-09-13T09:00", "2026-09-14T21:00"

PRICE_COLUMNS = ["sku", "captured_at", "price", "regular_price", "savings",
                 "availability", "seller", "stock_hint", "note"]


def snap(sku, at, price=None, **extra):
    """One snapshot row with the quiet defaults a normal listing has."""
    row = {"sku": sku, "captured_at": at, "price": price,
           "regular_price": None, "savings": None,
           "availability": "Add to cart", "seller": "Best Buy",
           "stock_hint": None, "note": None}
    row.update(extra)
    return row


def prices(rows):
    frame = pd.DataFrame(rows or [{}]).reindex(columns=PRICE_COLUMNS)
    if not rows:
        frame = frame.iloc[0:0]
    frame["captured_at"] = pd.to_datetime(frame["captured_at"])
    return frame


def products(rows):
    return pd.DataFrame(rows, columns=["sku", "role", "brand", "model_name",
                                       "list_price"])


BOTH_STRICT = products([(LENOVO, "strict", "Lenovo", "Yoga 7a", 1299.99),
                        (HP, "strict", "HP", "OmniBook X Flip", 1299.99),
                        (DELL, "reference", "Dell", "14S", 1299.99)])


def kinds(found):
    return [insight.kind for insight in found]


def only(found, kind):
    matches = [insight for insight in found if insight.kind == kind]
    assert matches, f"no {kind} insight was detected"
    return matches[0]


# --- price_change -------------------------------------------------------

def test_price_change_fires_on_a_move_against_the_same_sku():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99)]), BOTH_STRICT)
    change = only(found, "price_change")
    assert change.subjects == (LENOVO,)
    assert change.facts["previous_price"] == 1299.99
    assert change.facts["price"] == 1199.99
    assert change.facts["change"] == -100.00


def test_price_change_is_silent_when_the_price_is_unchanged():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, 1299.99)]), BOTH_STRICT)
    assert "price_change" not in kinds(found)


def test_price_change_is_silent_when_one_observation_has_no_price():
    """A missing price is not a move to zero and not a move from one."""
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, None, availability="Sold Out")]),
                   BOTH_STRICT)
    assert "price_change" not in kinds(found)


# --- parity -------------------------------------------------------------

def test_parity_held_fires_when_the_pair_matched_at_every_shared_time():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1299.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    held = only(found, "parity_held")
    assert held.facts["shared_times"] == 2
    assert held.facts["price"] == 1299.99
    assert "parity_broken" not in kinds(found)


def test_parity_held_is_silent_when_one_shared_time_disagreed():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    assert "parity_held" not in kinds(found)


def test_parity_broken_fires_when_a_later_shared_time_disagreed():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    broken = only(found, "parity_broken")
    assert broken.facts["previous_price"] == 1299.99
    assert broken.facts["gap"] == 100.00
    assert broken.at == pd.Timestamp(T3)


def test_parity_broken_is_silent_when_the_pair_was_never_level():
    found = detect(prices([snap(LENOVO, T1, 1249.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    assert "parity_broken" not in kinds(found)


def test_the_pair_kinds_need_both_strict_skus_priced_at_one_moment():
    """Regression: pairing each SKU's own nearest row would invent a moment
    at which the two were observed together."""
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    assert "parity_held" not in kinds(found)
    assert "parity_broken" not in kinds(found)


# --- the gap ------------------------------------------------------------

def test_gap_widened_fires_when_the_pair_drifted_apart():
    found = detect(prices([snap(LENOVO, T1, 1249.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    widened = only(found, "gap_widened")
    assert widened.facts["previous_gap"] == 50.00
    assert widened.facts["gap"] == 100.00
    assert "gap_narrowed" not in kinds(found)


def test_gap_narrowed_fires_when_the_pair_closed_up():
    found = detect(prices([snap(LENOVO, T1, 1199.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1249.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    narrowed = only(found, "gap_narrowed")
    assert narrowed.facts["previous_gap"] == 100.00
    assert narrowed.facts["gap"] == 50.00
    assert "gap_widened" not in kinds(found)


def test_neither_gap_kind_fires_when_the_gap_stood_still():
    found = detect(prices([snap(LENOVO, T1, 1249.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1149.99), snap(HP, T3, 1199.99)]),
                   BOTH_STRICT)
    assert "gap_widened" not in kinds(found)
    assert "gap_narrowed" not in kinds(found)


# --- promotions ---------------------------------------------------------

def test_promotion_active_fires_when_a_regular_price_sits_above_the_price():
    found = detect(prices([snap(DELL, T1, 999.99, regular_price=1299.99,
                                savings=300.00)]), BOTH_STRICT)
    promo = only(found, "promotion_active")
    assert promo.facts["discount"] == 300.00
    assert promo.facts["regular_price"] == 1299.99


def test_promotion_active_is_silent_with_no_regular_price():
    found = detect(prices([snap(DELL, T1, 999.99)]), BOTH_STRICT)
    assert "promotion_active" not in kinds(found)


def test_promotion_active_is_silent_when_the_regular_price_is_not_above():
    found = detect(prices([snap(DELL, T1, 999.99, regular_price=999.99)]),
                   BOTH_STRICT)
    assert "promotion_active" not in kinds(found)


def test_promotion_active_does_not_explain_anything():
    """The discount is reported. It is never offered as the reason for a gap."""
    found = detect(prices([snap(DELL, T1, 999.99, regular_price=1299.99)]),
                   BOTH_STRICT)
    sentence = only(found, "promotion_active").sentence.lower()
    for word in ("because", "explains", "due to", "driven by", "caused"):
        assert word not in sentence


def test_promotion_ending_fires_on_a_stated_end_date():
    note = "DEAL ENDS SEP 14 2026 (stated on page); pickup ready today; 5.0 (6)"
    found = detect(prices([snap(DELL, T1, 999.99, note=note)]), BOTH_STRICT)
    assert only(found, "promotion_ending").facts["ends"] == "SEP 14 2026"


def test_promotion_ending_is_silent_on_a_note_with_no_date():
    found = detect(prices([snap(DELL, T1, 999.99,
                                note="Pickup ready today; shipping unavailable")]),
                   BOTH_STRICT)
    assert "promotion_ending" not in kinds(found)


def test_promotion_ending_is_silent_when_the_note_only_says_ends_soon():
    """"Soon" is not a date, and reporting it as one would state a deadline
    the page never gave."""
    found = detect(prices([snap(DELL, T1, 999.99, note="Sale ends soon")]),
                   BOTH_STRICT)
    assert "promotion_ending" not in kinds(found)


@pytest.mark.parametrize("note, expected", [
    ("DEAL ENDS SEP 14 2026", "SEP 14 2026"),
    ("offer ends 2026-09-14", "2026-09-14"),
    ("Sale ends 9/14", "9/14"),
    ("ends on Sep 14", "Sep 14"),
    ("ends soon", None),
    ("", None),
    (None, None),
])
def test_stated_end_date_reads_only_a_real_date(note, expected):
    assert stated_end_date(note) == expected


# --- stock and availability ---------------------------------------------

def test_stock_scarcity_fires_on_a_non_empty_hint():
    hint = "Act fast - Only 1 left at your store!"
    found = detect(prices([snap(LENOVO, T1, 1299.99, stock_hint=hint)]),
                   BOTH_STRICT)
    assert only(found, "stock_scarcity").facts["stock_hint"] == hint


def test_stock_scarcity_is_silent_on_a_blank_hint():
    found = detect(prices([snap(LENOVO, T1, 1299.99, stock_hint="  ")]),
                   BOTH_STRICT)
    assert "stock_scarcity" not in kinds(found)


def test_a_changed_stock_note_is_reported_as_a_movement():
    """A window can hold no price movement and still hold movement. The note
    moving from one store to another is the observation a price-only page
    drops, and it was the first thing that moved on the real captures."""
    found = detect(prices([
        snap(DELL, T1, 999.99, stock_hint="Only 1 left at your store!"),
        snap(DELL, T3, 999.99, stock_hint="Only 1 left at nearby store!")]),
        BOTH_STRICT)
    shift = only(found, "field_shift")
    assert shift.facts["field"] == "stock_hint"
    assert shift.facts["previous_value"] == "Only 1 left at your store!"
    assert shift.facts["value"] == "Only 1 left at nearby store!"


def test_a_value_that_appears_or_goes_away_is_also_a_movement():
    """Blank is a value here. A note that was there and is not says as much as
    a note that changed wording."""
    found = detect(prices([snap(DELL, T1, 999.99),
                           snap(DELL, T3, 999.99, stock_hint="Only 1 left!")]),
                   BOTH_STRICT)
    assert only(found, "field_shift").facts["previous_value"] == ""


def test_an_unchanged_value_is_not_a_movement():
    found = detect(prices([snap(DELL, T1, 999.99, stock_hint="Only 1 left!"),
                           snap(DELL, T3, 999.99, stock_hint="Only 1 left!")]),
                   BOTH_STRICT)
    assert "field_shift" not in kinds(found)


def test_a_column_nobody_listed_is_watched_from_its_first_change():
    """The detector compares every descriptive column rather than a list of
    the ones known today. pickup_eta was added to the landing file mid-window,
    and its slip from today to Thu Sep 17 was the movement nothing could see
    while it lived in the free-text note."""
    frame = prices([snap(DELL, T1, 999.99), snap(DELL, T3, 999.99)])
    frame["pickup_eta"] = ["today", "2026-09-17"]
    shift = only(detect(frame, BOTH_STRICT), "field_shift")
    assert shift.facts["field"] == "pickup_eta"
    assert "pickup date" in shift.sentence


def test_prose_and_fields_with_their_own_detector_are_not_repeated():
    """A reworded note is not a movement, and a price change is already
    reported by the price detector with a sharper sentence."""
    found = detect(prices([snap(DELL, T1, 999.99, note="first wording"),
                           snap(DELL, T3, 899.99, note="second wording")]),
                   BOTH_STRICT)
    assert "field_shift" not in kinds(found)
    assert "price_change" in kinds(found)


def test_availability_lost_fires_when_the_cart_button_went_away():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, None, availability="Sold Out")]),
                   BOTH_STRICT)
    lost = only(found, "availability_lost")
    assert lost.facts["previous_availability"] == "Add to cart"
    assert lost.facts["availability"] == "Sold Out"


def test_availability_lost_is_silent_while_the_listing_stays_buyable():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, 1299.99)]), BOTH_STRICT)
    assert "availability_lost" not in kinds(found)


def test_availability_lost_is_silent_when_a_listing_came_back():
    found = detect(prices([snap(LENOVO, T1, None, availability="Sold Out"),
                           snap(LENOVO, T3, 1299.99)]), BOTH_STRICT)
    assert "availability_lost" not in kinds(found)


# --- no_change is a finding, not a fallback -----------------------------

def test_no_change_fires_across_two_capture_times_at_identical_prices():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1299.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    quiet = only(found, "no_change")
    assert quiet.facts["capture_times"] == 2
    assert quiet.facts["first_at"] == T1
    assert quiet.facts["at"] == T3


def test_no_change_does_not_fire_when_a_strict_price_moved():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    assert "no_change" not in kinds(found)


def test_no_change_needs_two_capture_times():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99)]),
                   BOTH_STRICT)
    assert "no_change" not in kinds(found)


def test_no_change_fires_alongside_other_findings():
    """It is a finding in its own right, so it is not suppressed by company."""
    hint = "Act fast - Only 1 left at your store!"
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1299.99, stock_hint=hint),
                           snap(HP, T3, 1299.99)]), BOTH_STRICT)
    assert "no_change" in kinds(found)
    assert "stock_scarcity" in kinds(found)


def test_a_reference_product_moving_does_not_silence_no_change():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(DELL, T1, 999.99),
                           snap(LENOVO, T3, 1299.99), snap(HP, T3, 1299.99),
                           snap(DELL, T3, 899.99)]), BOTH_STRICT)
    assert "no_change" in kinds(found)


# --- ranking ------------------------------------------------------------

def test_parity_broken_outranks_parity_held_at_equal_factors():
    strict_at_one_time = {"roles": ("strict", "strict")}
    broken = score("parity_broken", **strict_at_one_time)["significance"]
    held = score("parity_held", **strict_at_one_time)["significance"]
    assert broken > held


def test_a_strict_product_outranks_a_reference_one_at_equal_magnitude():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(DELL, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99), snap(HP, T3, 1299.99),
                           snap(DELL, T3, 1199.99)]), BOTH_STRICT)
    moves = {i.subjects[0]: i for i in found if i.kind == "price_change"}
    assert moves[LENOVO].facts["change"] == moves[DELL].facts["change"]
    assert moves[LENOVO].facts["magnitude"] == moves[DELL].facts["magnitude"]
    assert moves[LENOVO].significance > moves[DELL].significance


def test_a_change_outranks_a_non_change_of_the_same_scope():
    hint = "Act fast - Only 1 left at your store!"
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99, stock_hint=hint)]),
                   BOTH_STRICT)
    assert (only(found, "price_change").significance
            > only(found, "stock_scarcity").significance)


def test_a_later_observation_outranks_an_earlier_one():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T2, 1199.99),
                           snap(LENOVO, T3, 1299.99)]), BOTH_STRICT)
    moves = sorted((i for i in found if i.kind == "price_change"),
                   key=lambda i: i.at)
    assert moves[0].facts["recency"] < moves[1].facts["recency"]
    assert moves[0].significance < moves[1].significance


def test_detection_is_ordered_by_significance_descending():
    hint = "Act fast - Only 1 left at your store!"
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(DELL, T1, 1299.99, regular_price=1499.99),
                           snap(LENOVO, T3, 1199.99, stock_hint=hint),
                           snap(HP, T3, 1299.99),
                           snap(DELL, T3, 999.99, regular_price=1499.99)]),
                   BOTH_STRICT)
    assert len(found) > 3
    scores = [insight.significance for insight in found]
    assert scores == sorted(scores, reverse=True)


def test_the_four_factors_are_on_the_facts_and_multiply_to_the_score():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99)]), BOTH_STRICT)
    facts = only(found, "price_change").facts
    product = (facts["magnitude"] * facts["recency"]
               * facts["scope_weight"] * facts["rarity"])
    assert facts["significance"] == pytest.approx(product, abs=1e-6)


def test_every_significance_is_inside_zero_to_one():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(DELL, T1, 99.99, regular_price=9999.99),
                           snap(LENOVO, T3, 99.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    assert found
    assert all(0.0 <= insight.significance <= 1.0 for insight in found)


def test_magnitude_falls_back_to_a_base_for_a_kind_with_no_figure():
    assert magnitude(None, 1299.99) == magnitude(50.0, None) > 0.0


def test_magnitude_saturates_rather_than_running_away():
    assert magnitude(1299.99, 1299.99) == 1.0


def test_recency_is_flat_when_the_window_holds_one_moment():
    assert recency(T1, T1, T1) == 1.0


def test_scope_weight_takes_the_strongest_role_present():
    assert scope_weight(("reference",)) == 0.5
    assert scope_weight(("strict",)) == 1.0
    assert scope_weight(("reference", "strict")) == 1.0


# --- scope --------------------------------------------------------------

def test_scope_filters_by_sku():
    frame = prices([snap(LENOVO, T1, 1299.99), snap(LENOVO, T3, 1199.99),
                    snap(DELL, T1, 999.99), snap(DELL, T3, 899.99)])
    found = detect(frame, BOTH_STRICT, Scope(skus=(LENOVO,)))
    assert {i.subjects[0] for i in found} == {LENOVO}


def test_scope_filters_by_start():
    frame = prices([snap(LENOVO, T1, 1299.99), snap(LENOVO, T2, 1249.99),
                    snap(LENOVO, T3, 1199.99)])
    found = detect(frame, BOTH_STRICT, Scope(start=datetime(2026, 9, 13, 0, 0)))
    moves = [i for i in found if i.kind == "price_change"]
    assert len(moves) == 1
    assert moves[0].facts["previous_at"] == T2


def test_scope_filters_by_end():
    frame = prices([snap(LENOVO, T1, 1299.99), snap(LENOVO, T2, 1249.99),
                    snap(LENOVO, T3, 1199.99)])
    found = detect(frame, BOTH_STRICT, Scope(end=datetime(2026, 9, 13, 12, 0)))
    moves = [i for i in found if i.kind == "price_change"]
    assert len(moves) == 1
    assert moves[0].facts["at"] == T2


def test_scope_key_differs_when_the_selection_differs():
    base = Scope()
    assert base.key() != Scope(skus=(LENOVO,)).key()
    assert Scope(skus=(LENOVO,)).key() != Scope(skus=(LENOVO, HP)).key()
    assert base.key() != Scope(start=datetime(2026, 9, 13)).key()
    assert base.key() != Scope(end=datetime(2026, 9, 13)).key()
    assert (Scope(start=datetime(2026, 9, 13)).key()
            != Scope(end=datetime(2026, 9, 13)).key())


def test_scope_key_is_stable_across_sku_ordering():
    """The selection is a set, so two orderings of it are not a change."""
    assert Scope(skus=(LENOVO, HP)).key() == Scope(skus=(HP, LENOVO)).key()


def test_detection_on_an_empty_frame_finds_nothing():
    assert detect(prices([]), BOTH_STRICT) == []


def test_a_scope_that_admits_nothing_finds_nothing():
    frame = prices([snap(LENOVO, T1, 1299.99)])
    assert detect(frame, BOTH_STRICT, Scope(skus=("nope",))) == []


# --- rendering ----------------------------------------------------------

def test_the_sentence_carries_the_figures_from_the_facts():
    found = detect(prices([snap(LENOVO, T1, 1299.99),
                           snap(LENOVO, T3, 1199.99)]), BOTH_STRICT)
    change = only(found, "price_change")
    assert "$1,299.99" in change.sentence
    assert "$1,199.99" in change.sentence
    assert "$100.00" in change.sentence
    assert T1 in change.sentence and T3 in change.sentence
    assert "Lenovo Yoga 7a" in change.sentence


def test_the_promotion_sentence_carries_its_own_figures():
    found = detect(prices([snap(DELL, T1, 999.99, regular_price=1299.99)]),
                   BOTH_STRICT)
    sentence = only(found, "promotion_active").sentence
    assert "$999.99" in sentence
    assert "$1,299.99" in sentence
    assert "$300.00" in sentence


def test_the_no_change_sentence_carries_its_count_and_its_window():
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(LENOVO, T3, 1299.99), snap(HP, T3, 1299.99)]),
                   BOTH_STRICT)
    sentence = only(found, "no_change").sentence
    assert "2 capture times" in sentence
    assert T1 in sentence and T3 in sentence


def test_every_sentence_is_what_its_own_template_produces():
    hint = "Act fast - Only 1 left at your store!"
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(DELL, T1, 1299.99, regular_price=1499.99,
                                note="DEAL ENDS SEP 14 2026"),
                           snap(LENOVO, T3, 1199.99, stock_hint=hint),
                           snap(HP, T3, 1299.99),
                           snap(DELL, T3, 999.99, regular_price=1499.99,
                                availability="Sold Out")]), BOTH_STRICT)
    assert found
    for insight in found:
        assert insight.sentence == render(insight.kind, insight.facts)
        assert insight.sentence.endswith(".")


def test_every_declared_kind_has_a_template():
    from tracker.insights import RARITY

    assert set(RARITY) == set(RENDERERS)


def test_rendering_an_unknown_kind_refuses_rather_than_guessing():
    with pytest.raises(KeyError):
        render("invented_kind", {})


# --- stage two: the model selects ---------------------------------------

class FakeCompletions:
    """Hands back the next canned reply and records how it was called."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=self.replies[min(len(self.calls) - 1,
                                     len(self.replies) - 1)]))])


class FakeClient:
    def __init__(self, *replies):
        self.chat = SimpleNamespace(completions=FakeCompletions(replies))

    @property
    def calls(self):
        return self.chat.completions.calls


def candidates():
    hint = "Act fast - Only 1 left at your store!"
    found = detect(prices([snap(LENOVO, T1, 1299.99), snap(HP, T1, 1299.99),
                           snap(DELL, T1, 1299.99, regular_price=1499.99),
                           snap(LENOVO, T2, 1249.99, stock_hint=hint),
                           snap(HP, T2, 1299.99),
                           snap(LENOVO, T3, 1199.99),
                           snap(HP, T3, 1299.99),
                           snap(DELL, T3, 999.99, regular_price=1499.99)]),
                  BOTH_STRICT)
    assert len(found) > 3
    return found


def test_select_by_score_takes_the_top_k_with_no_client_at_all():
    found = candidates()
    picked = select_by_score(found, k=3)
    assert picked.source == "score"
    assert picked.framing is None
    assert list(picked.insights) == found[:3]


def test_select_by_score_never_asks_for_more_than_it_has():
    found = candidates()
    assert len(select_by_score(found, k=99).insights) == len(found)


def test_select_with_model_returns_only_candidates_that_were_offered():
    found = candidates()
    client = FakeClient('{"indices": [2, 0], "framing": null}')
    picked = select_with_model(found, client, "some-model", k=3)
    assert picked.source == "model"
    assert list(picked.insights) == [found[2], found[0]]
    assert all(insight in found for insight in picked.insights)


def test_the_model_is_shown_indices_kinds_scores_and_sentences_only():
    found = candidates()
    client = FakeClient('{"indices": [0], "framing": null}')
    select_with_model(found, client, "some-model")
    payload = client.calls[0]["messages"][-1]["content"]
    assert set(json.loads(payload)[0]) == {
        "index", "kind", "significance", "sentence"}


def test_an_out_of_range_index_falls_back_to_score_ordering():
    found = candidates()
    client = FakeClient('{"indices": [0, 999], "framing": "look here"}')
    picked = select_with_model(found, client, "some-model", k=3)
    assert picked.source == "score"
    assert list(picked.insights) == found[:3]
    assert picked.framing is None


def test_a_negative_index_falls_back_too():
    found = candidates()
    client = FakeClient('{"indices": [-1], "framing": null}')
    assert select_with_model(found, client, "some-model").source == "score"


def test_a_reply_that_is_not_json_falls_back():
    found = candidates()
    client = FakeClient("I think the first one is most interesting.")
    picked = select_with_model(found, client, "some-model", k=2)
    assert picked.source == "score"
    assert list(picked.insights) == found[:2]


def test_an_empty_selection_falls_back():
    found = candidates()
    client = FakeClient('{"indices": [], "framing": null}')
    assert select_with_model(found, client, "some-model").source == "score"


def test_a_selection_longer_than_k_falls_back():
    found = candidates()
    client = FakeClient('{"indices": [0, 1, 2, 3], "framing": null}')
    assert select_with_model(found, client, "some-model", k=3).source == "score"


def test_a_client_that_raises_falls_back():
    class Exploding:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            raise RuntimeError("no endpoint answered")

    found = candidates()
    picked = select_with_model(found, Exploding(), "some-model", k=2)
    assert picked.source == "score"
    assert list(picked.insights) == found[:2]


def test_a_framing_line_with_an_invented_figure_is_dropped():
    """The selection came from a validated list and survives. The sentence
    holding a figure nobody computed does not."""
    found = candidates()
    client = FakeClient(
        '{"indices": [1, 0], "framing": "Total exposure is $8,742.13."}')
    picked = select_with_model(found, client, "some-model", k=3)
    assert picked.framing is None
    assert picked.source == "model"
    assert list(picked.insights) == [found[1], found[0]]


def test_a_grounded_framing_line_survives():
    found = candidates()
    line = "Two of these sit around $1,299.99."
    client = FakeClient('{"indices": [0], "framing": "%s"}' % line)
    picked = select_with_model(found, client, "some-model")
    assert picked.framing == line
    assert picked.source == "model"


def test_a_framing_line_with_no_figure_at_all_survives():
    found = candidates()
    client = FakeClient(
        '{"indices": [0], "framing": "The strict pair is the thing to watch."}')
    picked = select_with_model(found, client, "some-model")
    assert picked.framing == "The strict pair is the thing to watch."


def test_a_blank_framing_line_becomes_none():
    found = candidates()
    client = FakeClient('{"indices": [0], "framing": "   "}')
    assert select_with_model(found, client, "some-model").framing is None


def test_select_with_model_on_an_empty_candidate_list_never_calls_out():
    client = FakeClient('{"indices": [0], "framing": null}')
    picked = select_with_model([], client, "some-model")
    assert picked.insights == ()
    assert picked.source == "score"
    assert client.calls == []


def test_a_duplicated_index_falls_back():
    found = candidates()
    client = FakeClient('{"indices": [0, 0], "framing": null}')
    assert select_with_model(found, client, "some-model").source == "score"


def test_an_insight_is_frozen():
    insight = Insight(kind="no_change", subjects=(LENOVO,), at=None)
    with pytest.raises(Exception):
        insight.kind = "price_change"


# --- reading order is a preference, not a score --------------------------

def four_captures():
    """Enough to raise findings of more than one group."""
    return detect(prices([
        snap(LENOVO, T1, 1299.99, stock_hint="Only 1 left!"),
        snap(HP, T1, 1299.99),
        snap(DELL, T1, 999.99, regular_price=1299.99, savings=300.0),
        snap(LENOVO, T3, 1299.99),
        snap(HP, T3, 1299.99),
        snap(DELL, T3, 999.99, regular_price=1299.99, savings=300.0),
    ]), BOTH_STRICT)


def test_a_stated_preference_leads_without_hiding_anything():
    """A preference says what to read first, not what to keep. A reader who
    asks for availability still sees the price findings under it and cannot be
    misled by their absence."""
    found = four_captures()
    reordered = prefer(found, ["Availability and stock"])
    assert len(reordered) == len(found)
    assert set(kinds(reordered)) == set(kinds(found))
    wanted = set(KIND_GROUPS["Availability and stock"])
    assert reordered[0].kind in wanted


def test_choosing_everything_changes_nothing():
    """The default page is the one the score alone produces. The control has
    to be used before it does anything."""
    found = four_captures()
    assert prefer(found, list(KIND_GROUPS)) == found


def test_choosing_nothing_changes_nothing():
    found = four_captures()
    assert prefer(found, []) == found


def test_score_still_breaks_ties_inside_a_group():
    """Preference decides which block comes first, not the order within it."""
    found = four_captures()
    led = prefer(found, ["Availability and stock"])
    wanted = set(KIND_GROUPS["Availability and stock"])
    front = [i for i in led if i.kind in wanted]
    assert [i.significance for i in front] == sorted(
        (i.significance for i in front), reverse=True)


def test_an_unknown_group_name_is_ignored_rather_than_obeyed():
    found = four_captures()
    assert prefer(found, ["Nonsense"]) == found
