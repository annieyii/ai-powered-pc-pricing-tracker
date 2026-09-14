"""Extraction, validation and scoring tests.

No test reaches the network. The client is a stub with the same
``chat.completions.create(...)`` shape the SDK exposes, returning canned replies,
so the model's behaviour is something each test states rather than discovers.
"""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracker.extract import (
    ExtractedSpec,
    ExtractionError,
    Finding,
    MissingSetting,
    extract,
    form_factor_in_enum,
    ram_is_power_of_two,
    read_settings,
    review_path,
    score,
    screen_inch_in_range,
    storage_in_known_set,
    validate,
    value_grounded_in_source,
)

RAW_TEXT = """Acme Nimbus 14 2-in-1 Laptop - 14" 2K OLED Touch Screen - Aria 5 430 Processor - 16GB Memory - 512GB SSD - Slate

Specifications
Brand: Acme
Product Name: Nimbus 14 2-in-1
Screen Size: 14 inches
Display Type: OLED
Screen Resolution: 1920 x 1200
Touch Screen: Yes
Processor Model Number: Aria 5 430
System Memory (RAM): 16 gigabytes
Total Storage Capacity: 512 gigabytes
Operating System: Windows 11 Home
Product Type: Laptop
Form Factor: 2-in-1
"""

GOOD_SPEC = {
    "brand": "Acme",
    "model_name": "Nimbus 14 2-in-1",
    "cpu": "Aria 5 430",
    "ram_gb": 16,
    "storage_gb": 512,
    "screen_inch": 14,
    "display_type": "OLED",
    "form_factor": "2-in-1",
    "operating_system": "Windows 11 Home",
    "device_type": "Laptop",
    "touch_screen": True,
    "screen_resolution": "1920x1200",
}

TRUTH_ROW = {
    "sku": "1234567",
    "brand": "Acme",
    "model_name": "Nimbus 14 2-in-1",
    "cpu": "Aria 5 430",
    "ram_gb": "16",
    "storage_gb": "512",
    "screen_inch": "14",
    "display_type": "OLED",
    "form_factor": "2-in-1",
    "operating_system": "",
    "device_type": "Laptop",
    "touch_screen": "Yes",
    "screen_resolution": "1920x1200",
}

ENV = {"LLM_BASE_URL": "https://endpoint.invalid/v1",
       "LLM_MODEL": "some-model",
       "LLM_API_KEY": "unused-by-some-servers"}


def spec_with(**overrides):
    return ExtractedSpec(**{**GOOD_SPEC, **overrides})


class FakeCompletions:
    """Hands back the next canned reply and records how it was called."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, *replies):
        self.chat = SimpleNamespace(completions=FakeCompletions(replies))

    @property
    def calls(self):
        return self.chat.completions.calls


# --- the schema is the boundary -----------------------------------------

def test_a_well_formed_response_parses_into_a_spec():
    client = FakeClient(json.dumps(GOOD_SPEC))
    spec = extract(RAW_TEXT, client, "some-model")
    assert isinstance(spec, ExtractedSpec)
    assert (spec.ram_gb, spec.storage_gb, spec.screen_inch) == (16, 512, 14.0)
    assert spec.touch_screen is True
    assert len(client.calls) == 1


def test_a_response_missing_a_required_field_is_rejected():
    without_cpu = {k: v for k, v in GOOD_SPEC.items() if k != "cpu"}
    with pytest.raises(ValidationError):
        ExtractedSpec(**without_cpu)


def test_prose_around_the_json_is_not_accepted_as_a_spec():
    client = FakeClient("Sure! Here you go.", "still not json")
    with pytest.raises(ExtractionError):
        extract(RAW_TEXT, client, "some-model")


# --- retry is bounded at one --------------------------------------------

def test_a_parse_failure_is_retried_once_and_then_succeeds():
    client = FakeClient('{"brand": "Acme"}', json.dumps(GOOD_SPEC))
    spec = extract(RAW_TEXT, client, "some-model")
    assert spec.brand == "Acme"
    assert len(client.calls) == 2


def test_the_retry_is_told_what_failed():
    client = FakeClient('{"brand": "Acme"}', json.dumps(GOOD_SPEC))
    extract(RAW_TEXT, client, "some-model")
    retry_messages = client.calls[1]["messages"]
    assert len(retry_messages) == 4
    assert "failed validation" in retry_messages[-1]["content"]
    assert "cpu" in retry_messages[-1]["content"]


def test_a_second_failure_gives_up_rather_than_retrying_again():
    client = FakeClient('{"brand": "Acme"}', '{"brand": "Acme"}')
    with pytest.raises(ExtractionError):
        extract(RAW_TEXT, client, "some-model")
    assert len(client.calls) == 2, "one retry, never a loop"


# --- grounding is the anti-hallucination check --------------------------

def test_a_number_absent_from_the_raw_text_is_reported_as_ungrounded():
    findings = value_grounded_in_source(spec_with(ram_gb=32), RAW_TEXT)
    assert [f.field for f in findings] == ["ram_gb"]
    assert "32" in findings[0].message


def test_an_extraction_read_off_the_page_is_fully_grounded():
    assert value_grounded_in_source(spec_with(), RAW_TEXT) == []


def test_grounding_ignores_notation_differences_in_a_resolution():
    assert value_grounded_in_source(
        spec_with(screen_resolution="1920 x 1200"), RAW_TEXT) == []


def test_a_converted_capacity_the_page_never_stated_is_reported():
    findings = value_grounded_in_source(spec_with(storage_gb=1024), RAW_TEXT)
    assert [f.field for f in findings] == ["storage_gb"]


# --- deterministic range and whitelist checks ---------------------------

@pytest.mark.parametrize("ram_gb", [12, 6, 20, 0])
def test_memory_that_is_not_a_power_of_two_is_rejected(ram_gb):
    findings = ram_is_power_of_two(spec_with(ram_gb=ram_gb))
    assert len(findings) == 1 and findings[0].field == "ram_gb"


@pytest.mark.parametrize("ram_gb", [8, 16, 32])
def test_memory_that_is_a_power_of_two_is_accepted(ram_gb):
    assert ram_is_power_of_two(spec_with(ram_gb=ram_gb)) == []


@pytest.mark.parametrize("storage_gb", [500, 480, 1000, 513])
def test_storage_outside_the_known_set_is_rejected(storage_gb):
    findings = storage_in_known_set(spec_with(storage_gb=storage_gb))
    assert len(findings) == 1 and findings[0].field == "storage_gb"


@pytest.mark.parametrize("storage_gb", [128, 256, 512, 1024, 2048])
def test_storage_in_the_known_set_is_accepted(storage_gb):
    assert storage_in_known_set(spec_with(storage_gb=storage_gb)) == []


@pytest.mark.parametrize("screen_inch", [9.9, 0, 27, 18.1])
def test_a_screen_size_outside_the_range_is_rejected(screen_inch):
    findings = screen_inch_in_range(spec_with(screen_inch=screen_inch))
    assert len(findings) == 1 and findings[0].field == "screen_inch"


@pytest.mark.parametrize("screen_inch", [10, 14, 15.6, 18])
def test_a_screen_size_inside_the_range_is_accepted(screen_inch):
    assert screen_inch_in_range(spec_with(screen_inch=screen_inch)) == []


@pytest.mark.parametrize("form_factor", ["convertible", "tablet", "laptop", ""])
def test_a_form_factor_outside_the_enum_is_rejected(form_factor):
    findings = form_factor_in_enum(spec_with(form_factor=form_factor))
    assert len(findings) == 1 and findings[0].field == "form_factor"


@pytest.mark.parametrize("form_factor", ["2-in-1", "clamshell", "Clamshell"])
def test_a_form_factor_in_the_enum_is_accepted(form_factor):
    assert form_factor_in_enum(spec_with(form_factor=form_factor)) == []


def test_validate_reports_every_objection_it_finds():
    findings = validate(spec_with(ram_gb=12, storage_gb=500), RAW_TEXT)
    validators = {f.validator for f in findings}
    assert {"ram_is_power_of_two", "storage_in_known_set",
            "value_grounded_in_source"} <= validators
    assert all(isinstance(f, Finding) for f in findings)


def test_validate_passes_a_clean_extraction():
    assert validate(spec_with(), RAW_TEXT) == []


# --- scoring against the verified master --------------------------------

def results_by_field(spec):
    return {r.field: r for r in score(spec, TRUTH_ROW)}


def test_scoring_reports_a_match_when_the_extraction_agrees():
    results = results_by_field(spec_with())
    assert results["cpu"].match is True
    assert results["ram_gb"].match is True
    assert results["screen_resolution"].match is True


def test_scoring_reports_a_mismatch_when_the_extraction_disagrees():
    results = results_by_field(spec_with(cpu="Aria 7 840", ram_gb=8))
    assert results["cpu"].match is False
    assert results["cpu"].extracted == "Aria 7 840"
    assert results["cpu"].verified == "Aria 5 430"
    assert results["ram_gb"].match is False


@pytest.mark.parametrize("extracted,field", [
    ("oled", "display_type"),
    ("  OLED  ", "display_type"),
    ("1920X1200", "screen_resolution"),
])
def test_notation_differences_are_not_counted_as_disagreements(extracted, field):
    assert results_by_field(spec_with(**{field: extracted}))[field].match is True


def test_a_boolean_is_compared_against_the_word_the_master_uses():
    assert results_by_field(spec_with(touch_screen=True))["touch_screen"].match is True
    assert results_by_field(spec_with(touch_screen=False))["touch_screen"].match is False


def test_a_field_the_master_leaves_blank_is_reported_but_not_scored():
    result = results_by_field(spec_with())["operating_system"]
    assert result.match is None
    assert result.verified == ""
    assert result.extracted == "Windows 11 Home"


def test_every_requested_field_appears_in_the_score():
    assert {r.field for r in score(spec_with(), TRUTH_ROW)} == set(GOOD_SPEC)


# --- configuration refuses to guess -------------------------------------

@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"])
def test_a_missing_variable_fails_with_its_own_name(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(MissingSetting) as caught:
        read_settings(env)
    assert missing in str(caught.value)


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"])
def test_a_blank_variable_is_treated_as_missing(missing):
    with pytest.raises(MissingSetting) as caught:
        read_settings({**ENV, missing: "   "})
    assert missing in str(caught.value)


def test_a_complete_environment_is_read_without_defaults():
    settings = read_settings(ENV)
    assert settings.base_url == ENV["LLM_BASE_URL"]
    assert settings.model == ENV["LLM_MODEL"]


# --- the review file names the model it scored --------------------------

def test_two_endpoints_write_two_review_files(tmp_path):
    """A fixed name would let the second run delete the first run's evidence,
    and both runs are what the reported score rests on."""
    first = review_path("gpt-4o-mini", tmp_path)
    second = review_path("qwen2.5:7b", tmp_path)
    assert first != second
    assert first.name == "extraction_review.gpt-4o-mini.csv"
    # A colon is legal in a model name and unwelcome in a filename.
    assert second.name == "extraction_review.qwen2.5-7b.csv"


def test_an_unexpected_key_fails_at_the_boundary():
    """pydantic drops keys it was not expecting unless told not to, so a reply
    carrying an invented field parsed cleanly and the invention was never
    seen. The module docstring calls the schema the first of three gates."""
    with pytest.raises(ValidationError):
        ExtractedSpec.model_validate({**GOOD_SPEC, "warranty_years": 3})

