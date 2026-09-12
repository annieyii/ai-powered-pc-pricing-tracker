"""Offline structured extraction, judged against the verified product master.

The unstructured half of the problem is the specification block: the same fact
reaches the page as ``16GB Memory``, ``16 GB RAM`` or ``System Memory: 16GB``,
and the title routinely disagrees with the table below it. A language model is
good at that reading and bad at being trusted, so nothing it produces is taken
on faith.

Three gates stand between the model and any conclusion. Its reply is parsed
through a pydantic schema, so a malformed or invented shape fails at the
boundary instead of entering the data. The parsed values then meet
deterministic validators, of which grounding is the one that matters: a number
the raw text never contained was not read, it was guessed. What survives is
scored field by field against ``products.csv``, which was filled in by hand,
giving a stated denominator rather than a favourable example.

The model never writes to ``products.csv``. The verified master is the thing it
is measured against, so letting extraction edit it would destroy the only
reference the score has. Output goes to ``data/extraction_review.csv``, which
records both values side by side and leaves the correction to a person.

This module is never imported by the dashboard. It is run by hand, it is the
only place an endpoint is configured, and the app runs with none of it set.
"""
from __future__ import annotations

import csv
import os
import re
import sys
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parents[1]
RAW_SPECS_DIR = ROOT / "data" / "raw_specs"
PRODUCTS_CSV = ROOT / "data" / "structured" / "products.csv"
REVIEW_CSV = ROOT / "data" / "extraction_review.csv"

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")

KNOWN_STORAGE_GB = frozenset({128, 256, 512, 1024, 2048})
SCREEN_INCH_RANGE = (10.0, 18.0)
FORM_FACTORS = frozenset({"2-in-1", "clamshell"})

REVIEW_COLUMNS = ["sku", "field", "extracted", "verified", "match",
                  "validator_findings"]

SYSTEM_PROMPT = (
    "You read a retail product page and return its specifications as JSON. "
    "Copy values from the page; never infer, complete or correct them. "
    "When the title and the specification table disagree, the table wins. "
    "Return a single JSON object with exactly these keys: "
    "brand, model_name, cpu, ram_gb, storage_gb, screen_inch, display_type, "
    "form_factor, operating_system, device_type, touch_screen, "
    "screen_resolution. "
    "ram_gb and storage_gb are integers in gigabytes, screen_inch is a number "
    "in inches, touch_screen is a boolean, form_factor is either '2-in-1' or "
    "'clamshell', screen_resolution is formatted as 1920x1200."
)


class ExtractedSpec(BaseModel):
    """The fields the equivalence rule needs, and nothing else.

    Every field is required. An omission is a failed extraction rather than a
    null to be filled in later, and pydantic is where that is decided.
    """

    brand: str
    model_name: str
    cpu: str
    ram_gb: int
    storage_gb: int
    screen_inch: float
    display_type: str
    form_factor: str
    operating_system: str
    device_type: str
    touch_screen: bool
    screen_resolution: str


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class Finding:
    """One deterministic objection to a value the model produced."""

    field: str
    validator: str
    message: str


@dataclass(frozen=True)
class FieldResult:
    """One extracted field set beside the verified value for the same field.

    ``match`` is None where the master holds no verified value, which is not a
    disagreement and must not be counted as one.
    """

    field: str
    extracted: str
    verified: str
    match: bool | None


class MissingSetting(Exception):
    """A required environment variable was absent or blank."""


class ExtractionError(Exception):
    """The model failed to return a parsable specification."""


def read_settings(env: Mapping[str, str]) -> Settings:
    """Read the endpoint configuration, refusing to guess at any of it.

    There is no default base URL and no default model. The endpoint is remote
    and supplied at run time, so a fallback would name a host that does not
    answer or a model that was never pulled, and the failure would surface as a
    connection error far from its cause.
    """
    for name in REQUIRED_ENV:
        if not (env.get(name) or "").strip():
            raise MissingSetting(
                f"{name} is not set. {', '.join(REQUIRED_ENV)} are all required; "
                f"see .env.example. No endpoint or model is assumed.")
    return Settings(base_url=env["LLM_BASE_URL"].strip(),
                    model=env["LLM_MODEL"].strip(),
                    api_key=env["LLM_API_KEY"].strip())


def build_client(settings: Settings) -> OpenAI:
    """An OpenAI-compatible client, whatever is actually serving the model.

    Passing ``base_url`` is the whole of the portability story: the same call
    reaches a remote endpoint or a local Ollama instance, and no
    backend-specific branch exists anywhere in this module.
    """
    return OpenAI(base_url=settings.base_url, api_key=settings.api_key)


def extract(raw_text: str, client: Any, model: str) -> ExtractedSpec:
    """Turn one raw page dump into a validated ExtractedSpec.

    A reply that does not fit the schema is handed back to the model once, with
    the validation error as the correction. One retry, not a loop: a model that
    cannot produce the shape twice is not going to find it on the fifth attempt,
    and an unbounded retry quietly turns a broken endpoint into a large bill.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": raw_text},
    ]

    for attempt in range(2):
        reply = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = reply.choices[0].message.content or ""
        try:
            return ExtractedSpec.model_validate_json(content)
        except ValidationError as exc:
            if attempt == 1:
                raise ExtractionError(
                    f"the model did not return a valid specification after one "
                    f"retry: {exc}") from exc
            messages += [
                {"role": "assistant", "content": content},
                {"role": "user",
                 "content": f"That response failed validation:\n{exc}\n"
                            f"Return the corrected JSON object only."},
            ]

    raise AssertionError("unreachable")


def _digits(value: object) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.findall(r"\d+", str(value))


def value_grounded_in_source(spec: ExtractedSpec, raw_text: str) -> list[Finding]:
    """Every number in the extraction must occur in the text it came from.

    This is the anti-hallucination check and the reason the raw files are kept.
    A model that reports 32GB for a 16GB machine is not making a reading error
    that a range check would catch: 32 is a perfectly plausible memory size. It
    is caught only by noticing that 32 never appeared on the page.

    Digit runs are compared rather than whole strings, so ``1920 x 1200`` in the
    source grounds ``1920x1200`` in the extraction. A page that writes 1TB where
    the model reports 1024 is reported here too, correctly: that value was
    converted, not read, and the review file is where a person settles it.
    """
    source = set(re.findall(r"\d+", raw_text))
    findings: list[Finding] = []
    for field, value in spec.model_dump().items():
        for token in _digits(value):
            if token not in source:
                findings.append(Finding(
                    field, "value_grounded_in_source",
                    f"{token} does not appear in the raw text "
                    f"(extracted {field}={value!r})"))
    return findings


def ram_is_power_of_two(spec: ExtractedSpec) -> list[Finding]:
    ram = spec.ram_gb
    if ram > 0 and ram & (ram - 1) == 0:
        return []
    return [Finding("ram_gb", "ram_is_power_of_two",
                    f"{ram} GB is not a power of two")]


def storage_in_known_set(spec: ExtractedSpec) -> list[Finding]:
    if spec.storage_gb in KNOWN_STORAGE_GB:
        return []
    return [Finding("storage_gb", "storage_in_known_set",
                    f"{spec.storage_gb} GB is not one of "
                    f"{sorted(KNOWN_STORAGE_GB)}")]


def screen_inch_in_range(spec: ExtractedSpec) -> list[Finding]:
    low, high = SCREEN_INCH_RANGE
    if low <= spec.screen_inch <= high:
        return []
    return [Finding("screen_inch", "screen_inch_in_range",
                    f"{spec.screen_inch} in is outside {low}-{high} in")]


def form_factor_in_enum(spec: ExtractedSpec) -> list[Finding]:
    if spec.form_factor.strip().lower() in FORM_FACTORS:
        return []
    return [Finding("form_factor", "form_factor_in_enum",
                    f"{spec.form_factor!r} is not one of "
                    f"{sorted(FORM_FACTORS)}")]


def validate(spec: ExtractedSpec, raw_text: str) -> list[Finding]:
    """Run every validator against the model output.

    These judge the extraction, never the master. Running them over the
    verified rows would test the people who typed them, which is not the
    question being asked.
    """
    return [
        *value_grounded_in_source(spec, raw_text),
        *ram_is_power_of_two(spec),
        *storage_in_known_set(spec),
        *screen_inch_in_range(spec),
        *form_factor_in_enum(spec),
    ]


_BOOLEAN_WORDS = {"true": "yes", "yes": "yes", "false": "no", "no": "no"}
_UNIT_SUFFIX = re.compile(r'^(\d+(?:\.\d+)?)\s*(gb|tb|mb|in|inch|inches|")?$')


def _normalise(value: object) -> str:
    """Reduce a value to the form a comparison can use, and no further.

    Case, surrounding space, separators and a trailing unit are notation. A
    different processor model number or a different panel type is a
    disagreement, so nothing here touches the words themselves: no synonyms, no
    abbreviations, no fuzzy matching that could turn a wrong answer into a
    right one.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = re.sub(r"[\s_-]+", " ", str(value).strip().lower()).strip()
    if text in _BOOLEAN_WORDS:
        return _BOOLEAN_WORDS[text]

    match = _UNIT_SUFFIX.match(text.replace(",", ""))
    if match:
        number = float(match.group(1))
        if match.group(2) == "tb":
            number *= 1024
        return str(int(number)) if number.is_integer() else str(number)
    return text


def score(spec: ExtractedSpec, truth_row: Mapping[str, Any]) -> list[FieldResult]:
    """Compare the extraction against one verified row, field by field.

    Every field the model was asked for is reported, including the ones the
    master leaves blank. Dropping those would shrink the denominator to the
    fields that happen to be filled in, which flatters the score.
    """
    results: list[FieldResult] = []
    for field, extracted in spec.model_dump().items():
        verified = truth_row.get(field)
        verified_text = "" if verified is None else str(verified).strip()
        match = None if verified_text == "" else (
            _normalise(extracted) == _normalise(verified_text))
        results.append(FieldResult(field=field,
                                   extracted=str(_display(extracted)),
                                   verified=verified_text,
                                   match=match))
    return results


def _display(value: object) -> object:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_truth(csv_path: Path | str = PRODUCTS_CSV) -> dict[str, dict[str, str]]:
    """The human-verified master, keyed by SKU. Read only, never written."""
    with open(csv_path, newline="", encoding="utf-8") as handle:
        return {row["sku"]: row for row in csv.DictReader(handle)}


def raw_text_for(sku: str, directory: Path = RAW_SPECS_DIR) -> str | None:
    path = directory / f"{sku}.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def review_rows(sku: str, results: list[FieldResult],
                findings: list[Finding]) -> list[dict[str, str]]:
    by_field: dict[str, list[str]] = {}
    for finding in findings:
        by_field.setdefault(finding.field, []).append(
            f"{finding.validator}: {finding.message}")
    return [{"sku": sku,
             "field": result.field,
             "extracted": result.extracted,
             "verified": result.verified,
             "match": "" if result.match is None else str(result.match).lower(),
             "validator_findings": "; ".join(by_field.get(result.field, []))}
            for result in results]


def main() -> int:
    try:
        settings = read_settings(os.environ)
    except MissingSetting as exc:
        print(str(exc), file=sys.stderr)
        return 2

    truth = read_truth()
    client = build_client(settings)

    rows: list[dict[str, str]] = []
    compared = matched = fired = 0
    skipped: list[str] = []

    for sku, truth_row in truth.items():
        raw_text = raw_text_for(sku)
        if raw_text is None:
            skipped.append(sku)
            continue

        try:
            spec = extract(raw_text, client, settings.model)
        except ExtractionError as exc:
            print(f"{sku}: {exc}", file=sys.stderr)
            continue

        findings = validate(spec, raw_text)
        results = score(spec, truth_row)
        rows += review_rows(sku, results, findings)

        compared += sum(1 for r in results if r.match is not None)
        matched += sum(1 for r in results if r.match is True)
        fired += len(findings)

    if not rows:
        print(f"nothing extracted; no usable raw spec files in {RAW_SPECS_DIR}",
              file=sys.stderr)
        return 1

    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(REVIEW_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    products = len({row["sku"] for row in rows})
    print(f"{len(rows)} fields extracted across {products} "
          f"product{'' if products == 1 else 's'}; "
          f"{matched} of {compared} matched the verified value; "
          f"{fired} validator findings. "
          f"Written to {REVIEW_CSV.relative_to(ROOT)}.")
    if skipped:
        print(f"no raw text for {', '.join(sorted(skipped))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
