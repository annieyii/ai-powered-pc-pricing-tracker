"""Summary context, deterministic prose, grounding and the stored file.

No test reaches the network. The client is a stub with the same
``chat.completions.create(...)`` shape the SDK exposes, returning canned prose,
so what the model is supposed to have said is something each test states rather
than discovers.
"""
from types import SimpleNamespace

import pytest

from tracker.store import PRICE_CSV_COLUMNS, PRODUCT_FIELDS, build
from tracker.summarise import (
    StoredSummary,
    build_context,
    generate_summary,
    read_stored_summary,
    settings_or_reason,
    stored_is_stale,
    summarise,
    template_summary,
    verify_grounded,
    write_stored_summary,
)

ENV = {"LLM_BASE_URL": "https://endpoint.invalid/v1",
       "LLM_MODEL": "some-model",
       "LLM_API_KEY": "unused-by-some-servers"}

PRODUCTS = [
    # sku, role, brand, model_name, brightness
    ("1000001", "strict", "Acme", "Nimbus 14 2-in-1", 400),
    ("1000002", "strict", "Borealis", "Drift 14 2-in-1", 300),
    ("1000003", "reference", "Cirrus", "Vane 14", 300),
]

TWO_TIMES = [
    ("1000001", "2026-09-12T15:07", "1299.99", "", "", "Add to cart"),
    ("1000002", "2026-09-12T15:07", "1299.99", "", "", "Add to cart"),
    ("1000003", "2026-09-12T15:07", "999.99", "1299.99", "300.00", "Add to cart"),
    ("1000001", "2026-09-14T21:00", "1299.99", "", "", "Add to cart"),
    ("1000002", "2026-09-14T21:00", "1249.99", "1299.99", "50.00", "Add to cart"),
    ("1000003", "2026-09-14T21:00", "999.99", "1299.99", "300.00", "Add to cart"),
]

ONE_TIME = TWO_TIMES[:3]


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


def _product_line(sku, role, brand, model_name, brightness):
    # operating_system is stated rather than blank: the store refuses a strict
    # product that leaves a field the equivalence rule names unsaid, because
    # two blanks agree with each other and switch the check off.
    return (f"{sku},{role},{brand},{model_name},MN-{sku},Aria 5 430,16,512,14,"
            f"OLED,{brightness},2-in-1,Windows 11 Home,Laptop,"
            f"Acme Radeon 840M,Yes,1920x1200,1299.99,"
            f"https://example.invalid/{sku}")


def dataset_for(tmp_path, price_rows):
    products_csv = tmp_path / "products.csv"
    products_csv.write_text("\n".join(
        [",".join(PRODUCT_FIELDS)] + [_product_line(*p) for p in PRODUCTS]) + "\n")

    prices_csv = tmp_path / "prices.csv"
    lines = [",".join(PRICE_CSV_COLUMNS)]
    for sku, at, price, regular, savings, availability in price_rows:
        lines.append(f"{sku},{at},{price},{regular},{savings},{availability},"
                     f"Best Buy,,")
    prices_csv.write_text("\n".join(lines) + "\n")

    return build(products_csv, prices_csv)


@pytest.fixture
def context(tmp_path):
    data = dataset_for(tmp_path, TWO_TIMES)
    return build_context(data, data.products)


@pytest.fixture
def one_snapshot_context(tmp_path):
    data = dataset_for(tmp_path, ONE_TIME)
    return build_context(data, data.products)


@pytest.fixture
def empty_context(tmp_path):
    data = dataset_for(tmp_path, [])
    return build_context(data, data.products)


# --- the context is the whole of what the model may say -----------------

def test_the_context_counts_the_capture_times(context):
    assert context["capture_times"] == 2
    assert context["strict_capture_times"] == 2


def test_the_context_reports_the_first_and_last_capture_time(context):
    assert context["first_capture"] == "2026-09-12T15:07"
    assert context["last_capture"] == "2026-09-14T21:00"


def test_the_context_carries_the_latest_price_per_product(context):
    prices = {row["name"]: row["price"] for row in context["products"]}
    assert prices["Borealis Drift 14 2-in-1"] == 1249.99
    assert prices["Acme Nimbus 14 2-in-1"] == 1299.99


def test_the_context_carries_the_regular_price_savings_and_availability(context):
    row = next(r for r in context["products"]
               if r["name"] == "Borealis Drift 14 2-in-1")
    assert (row["regular_price"], row["savings"]) == (1299.99, 50.0)
    assert row["availability"] == "Add to cart"


def test_the_context_carries_the_strict_comparison(context):
    comparison = context["strict_comparison"]
    assert comparison["comparable"] is True
    assert comparison["captured_at"] == "2026-09-14T21:00"
    assert comparison["delta"] == 50.0
    assert comparison["cheaper"] == "Borealis Drift 14 2-in-1"


def test_the_context_reports_that_a_strict_price_moved(context):
    assert context["any_strict_price_changed"] is True


def test_the_context_reports_no_movement_from_a_single_snapshot(
        one_snapshot_context):
    assert one_snapshot_context["any_strict_price_changed"] is False
    assert one_snapshot_context["capture_times"] == 1


def test_the_context_holds_no_raw_rows_or_free_text(context):
    """Only computed values go to the model, never a seller's own wording."""
    flat = str(context)
    assert "stock_hint" not in flat and "note" not in flat


def test_every_product_stays_in_the_context(context):
    assert len(context["products"]) == len(PRODUCTS)


# --- the deterministic summary always works -----------------------------

def test_the_template_summary_describes_a_normal_dataset(context):
    prose = template_summary(context)
    assert "2 capture times" in prose
    assert "2026-09-14T21:00" in prose
    assert "$1,249.99" in prose


def test_the_template_summary_handles_a_single_snapshot(one_snapshot_context):
    prose = template_summary(one_snapshot_context)
    assert "A single capture time at 2026-09-12T15:07." in prose
    assert "no movement can be reported yet" in prose


def test_the_template_summary_handles_empty_data(empty_context):
    assert "nothing to summarise" in template_summary(empty_context)


def test_the_template_summary_claims_no_movement_that_did_not_happen(
        one_snapshot_context):
    prose = template_summary(one_snapshot_context).lower()
    assert "changed" not in prose


def test_the_template_summary_reports_a_price_that_did_move(context):
    assert "At least one strict price changed" in template_summary(context)


def test_the_template_summary_is_itself_fully_grounded(context):
    """It is built from the context, so it cannot state anything else."""
    assert verify_grounded(template_summary(context), context) == []


def test_the_wording_stays_a_matched_pair_observation(context):
    prose = template_summary(context).lower()
    assert "matched-pair observation" in prose
    assert "effect" not in prose and "premium" not in prose


# --- grounding -----------------------------------------------------------

def test_a_figure_absent_from_the_context_is_reported(context):
    ungrounded = verify_grounded(
        "The gap widened to $175.50 at the last capture.", context)
    assert ungrounded == ["175.50"]


def test_money_written_with_a_separator_is_recognised(context):
    assert verify_grounded("It was listed at $1,249.99.", context) == []


def test_a_rounded_figure_the_context_never_held_is_reported(context):
    assert verify_grounded("Roughly $1,250 on the day.", context) == ["1,250"]


def test_prose_with_no_figures_is_grounded(context):
    assert verify_grounded("Nothing moved worth reporting.", context) == []


def test_each_ungrounded_figure_is_reported_once(context):
    assert verify_grounded("It was 4.5, then 4.5 again, then 9.9.",
                           context) == ["4.5", "9.9"]


# --- the summary is never shown with an unverified figure ---------------

GROUNDED_PROSE = ("At 2026-09-14T21:00 the Borealis Drift 14 2-in-1 was listed "
                  "at $1,249.99 against $1,299.99 for the Acme Nimbus 14 2-in-1, "
                  "a matched-pair observation with a gap of $50.00. Worth a "
                  "human look: the Borealis discount.")

UNGROUNDED_PROSE = ("The Borealis Drift 14 2-in-1 has fallen 12.5 percent to "
                    "$1,137.49 and now undercuts the market.")


def test_a_fully_grounded_model_summary_is_used(context):
    client = FakeClient(GROUNDED_PROSE)
    prose, ungrounded, source = summarise(context, client, "some-model")
    assert source == "model"
    assert prose == GROUNDED_PROSE
    assert ungrounded == []
    assert len(client.calls) == 1


def test_a_summary_with_an_invented_figure_is_discarded_for_the_template(context):
    client = FakeClient(UNGROUNDED_PROSE)
    prose, ungrounded, source = summarise(context, client, "some-model")
    assert source == "template"
    assert prose == template_summary(context)
    assert "12.5" in ungrounded and "1,137.49" in ungrounded


def test_the_discarded_summary_leaves_no_trace_in_what_is_returned(context):
    prose, _, _ = summarise(context, FakeClient(UNGROUNDED_PROSE), "some-model")
    assert "1,137.49" not in prose and "undercuts" not in prose


def test_one_retry_happens_before_the_template_is_used(context):
    client = FakeClient(UNGROUNDED_PROSE, UNGROUNDED_PROSE)
    _, _, source = summarise(context, client, "some-model")
    assert source == "template"
    assert len(client.calls) == 2, "one retry, never a loop"


def test_a_retry_that_comes_back_grounded_is_accepted(context):
    client = FakeClient(UNGROUNDED_PROSE, GROUNDED_PROSE)
    prose, ungrounded, source = summarise(context, client, "some-model")
    assert (source, prose, ungrounded) == ("model", GROUNDED_PROSE, [])
    assert len(client.calls) == 2


def test_the_retry_is_told_which_figures_were_refused(context):
    client = FakeClient(UNGROUNDED_PROSE, GROUNDED_PROSE)
    summarise(context, client, "some-model")
    retry = client.calls[1]["messages"][-1]["content"]
    assert "1,137.49" in retry and "do not appear" in retry


# --- the prompt hands over the context and nothing else -----------------

def test_the_model_is_given_the_context_as_json(context):
    client = FakeClient(GROUNDED_PROSE)
    generate_summary(context, client, "some-model")
    messages = client.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "2026-09-14T21:00" in messages[1]["content"]


def test_the_prompt_forbids_figures_outside_the_context(context):
    client = FakeClient(GROUNDED_PROSE)
    generate_summary(context, client, "some-model")
    system = client.calls[0]["messages"][0]["content"].lower()
    assert "no figure that does not appear" in system
    assert "warrant a human look" in system


def test_the_prompt_forbids_claiming_movement_that_did_not_happen(context):
    client = FakeClient(GROUNDED_PROSE)
    generate_summary(context, client, "some-model")
    system = client.calls[0]["messages"][0]["content"].lower()
    assert "nothing changed" in system


# --- configuration is reported, never guessed ---------------------------

@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"])
def test_a_missing_variable_is_reported_without_raising(missing):
    settings, reason = settings_or_reason(
        {k: v for k, v in ENV.items() if k != missing})
    assert settings is None
    assert missing in reason


def test_no_variable_at_all_is_reported_without_raising():
    settings, reason = settings_or_reason({})
    assert settings is None and "LLM_BASE_URL" in reason


def test_a_complete_environment_is_read_without_defaults():
    settings, reason = settings_or_reason(ENV)
    assert reason is None
    assert (settings.base_url, settings.model) == (ENV["LLM_BASE_URL"],
                                                   ENV["LLM_MODEL"])


# --- the stored summary --------------------------------------------------

def test_no_stored_file_means_no_stored_summary(tmp_path):
    assert read_stored_summary(tmp_path / "absent.md") is None


def test_a_stored_summary_round_trips_with_its_header(tmp_path, context):
    path = tmp_path / "summary.md"
    write_stored_summary(GROUNDED_PROSE, "some-model", context, path)
    stored = read_stored_summary(path)
    assert stored.prose == GROUNDED_PROSE
    assert stored.model == "some-model"
    assert stored.last_capture == "2026-09-14T21:00"
    assert stored.generated_at != "unrecorded"


def test_a_stored_summary_is_stale_when_a_newer_snapshot_exists(context):
    stored = StoredSummary(prose="", model="m", generated_at="2026-09-12T16:00",
                           last_capture="2026-09-12T15:07")
    assert stored_is_stale(stored, context) is True


def test_a_stored_summary_generated_from_the_latest_capture_is_not_stale(context):
    stored = StoredSummary(prose="", model="m", generated_at="2026-09-14T22:00",
                           last_capture="2026-09-14T21:00")
    assert stored_is_stale(stored, context) is False


def test_staleness_is_not_claimed_when_there_is_nothing_to_compare(
        empty_context):
    stored = StoredSummary(prose="", model="m", generated_at="2026-09-12T16:00",
                           last_capture="2026-09-12T15:07")
    assert stored_is_stale(stored, empty_context) is False


def test_a_stored_file_without_a_header_is_still_readable(tmp_path):
    path = tmp_path / "summary.md"
    path.write_text("Prose someone wrote by hand.\n")
    stored = read_stored_summary(path)
    assert stored.prose == "Prose someone wrote by hand."
    assert stored.model == "unrecorded"
    assert stored_is_stale(stored, {"last_capture": "2026-09-14T21:00"}) is False
