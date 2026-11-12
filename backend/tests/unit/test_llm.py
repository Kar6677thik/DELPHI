"""The LLM module without a database (docs/15, #104): templates, validators, minimisation and
prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from delphi.kb.loader import load_and_validate
from delphi.llm.minimise import minimise, pseudonym
from delphi.llm.port import LlmRequest, LlmStatus, Purpose
from delphi.llm.prompts import load_prompt
from delphi.llm.schemas import (
    SCHEMAS,
    CaseSummary,
    EvidenceNarrative,
    QuestionVariant,
    QuestionVariants,
    VariantOption,
)
from delphi.llm.template import TemplateLlm, indian_grouping, render_narrative
from delphi.llm.validators import validate
from delphi.probes.registry import CLASSIFIER_NAMES
from tests.llm_payloads import NARRATIVES, payload

KB_DIR = Path(__file__).resolve().parents[3] / "kb"


def _request(purpose: Purpose, user_payload: dict[str, object]) -> LlmRequest:
    return LlmRequest(purpose, "v1", "system", user_payload, SCHEMAS[purpose], 1024)


def _narrative(finding: str, citations: list[str]) -> EvidenceNarrative:
    return EvidenceNarrative(finding=finding, citations=citations)


# ------------------------------------------------------------------------------- templates


def test_every_evidence_source_has_a_template() -> None:
    kb = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
    evidence = {k for k, s in kb.sources.items() if s.kind == "EVIDENCE"}
    assert evidence == set(NARRATIVES)


@pytest.mark.parametrize("source", sorted(NARRATIVES))
def test_each_template_passes_its_own_validation(source: str) -> None:
    """Templates are the reference output (docs/15 §7): what they write always validates."""
    result = TemplateLlm().generate(_request(Purpose.EVIDENCE_NARRATIVE, NARRATIVES[source]))
    assert result.status is LlmStatus.OK and isinstance(result.parsed, EvidenceNarrative)
    verdict = validate(Purpose.EVIDENCE_NARRATIVE, result.parsed, NARRATIVES[source])
    assert verdict.passed, (result.parsed.finding, verdict.failures)
    assert len(result.parsed.finding) <= 400


def test_templates_render_amounts_in_indian_grouping() -> None:
    text = render_narrative(NARRATIVES["PAYEE_RELATIONSHIP"])
    assert "₹4,80,000" in text and "[ev-payee_relationship]" in text


def test_the_other_branches_of_the_templates() -> None:
    quiet = payload(
        "PASS_THROUGH_DWELL",
        {"median_dwell_minutes": None, "credits": 0, "window_days": 7, "departure_share": 0.8},
    )
    assert "received no credits" in render_narrative(quiet)
    none = payload("LINKED_COMPLAINTS", {"complaints": 0})
    assert render_narrative(none).startswith("No complaint")
    usual = payload(
        "LOGIN_GEO_VELOCITY",
        {"last_city": "Pune", "speed_kmh": None, "new_city": False, "window_h": 24},
    )
    assert "usual locations" in render_narrative(usual)


@pytest.mark.parametrize(
    ("rupees", "text"),
    [
        (0, "0"),
        (999, "999"),
        (480000, "4,80,000"),
        (10_000_000, "1,00,00,000"),
        (1234.5, "1,234.50"),
    ],
)
def test_indian_grouping(rupees: float, text: str) -> None:
    assert indian_grouping(rupees) == text


def test_the_case_summary_joins_the_narratives_and_cites_them_all() -> None:
    records = [NARRATIVES["INBOUND_FAN_IN"], NARRATIVES["ACCOUNT_PROFILE"]]
    result = TemplateLlm().generate(_request(Purpose.CASE_SUMMARY, {"records": records}))
    assert isinstance(result.parsed, CaseSummary)
    assert result.parsed.citations == ["ev-inbound_fan_in", "ev-account_profile"]
    assert validate(Purpose.CASE_SUMMARY, result.parsed, {"records": records}).passed


def test_question_variants_have_no_template() -> None:
    result = TemplateLlm().generate(_request(Purpose.QUESTION_VARIANTS, {"options": []}))
    assert result.status is LlmStatus.DISABLED and result.parsed is None


def test_the_typology_template() -> None:
    stats = {
        "record_id": "cluster-7",
        "suggested_name": "Collect-request refunds",
        "stats": {"cases": 14, "traits": ["UPI collect requests", "refund wording"]},
    }
    result = TemplateLlm().generate(_request(Purpose.TYPOLOGY_DESCRIPTION, stats))
    assert result.status is LlmStatus.OK
    assert validate(Purpose.TYPOLOGY_DESCRIPTION, result.parsed, stats).passed


# ------------------------------------------------------------------------------ validators

FAN_IN = NARRATIVES["INBOUND_FAN_IN"]
CITE = ["ev-inbound_fan_in"]


def test_a_faithful_paraphrase_passes() -> None:
    text = "In the 48 h before the payment, 12 unrelated payers credited ACC-…5678."
    assert validate(Purpose.EVIDENCE_NARRATIVE, _narrative(text, CITE), FAN_IN).passed


def test_a_citation_must_exist() -> None:
    verdict = validate(Purpose.EVIDENCE_NARRATIVE, _narrative("12 payers.", ["ev-made-up"]), FAN_IN)
    assert not verdict.passed and "cites records not in the payload" in verdict.failures[0]


def test_a_wrong_number_fails() -> None:
    verdict = validate(
        Purpose.EVIDENCE_NARRATIVE, _narrative("13 payers credited it.", CITE), FAN_IN
    )
    assert not verdict.passed and "'13'" in verdict.failures[0]


def test_numbers_are_read_as_written() -> None:
    dwell = NARRATIVES["PASS_THROUGH_DWELL"]
    ok = "80% of each credit left within 17.5 minutes, over 9 credits in 7 days."
    assert validate(
        Purpose.EVIDENCE_NARRATIVE, _narrative(ok, ["ev-pass_through_dwell"]), dwell
    ).passed
    rounded = "Funds left after about 18 minutes."  # 17.5 at whole-minute precision
    assert validate(
        Purpose.EVIDENCE_NARRATIVE, _narrative(rounded, ["ev-pass_through_dwell"]), dwell
    ).passed
    payee = NARRATIVES["PAYEE_RELATIONSHIP"]
    for amount in ("₹4,80,000", "₹480,000", "480000"):
        text = f"The largest of 4 earlier payments was {amount}."
        assert validate(
            Purpose.EVIDENCE_NARRATIVE, _narrative(text, ["ev-payee_relationship"]), payee
        ).passed, amount


def test_dates_times_and_citation_brackets_are_not_numbers() -> None:
    text = "12 payers credited ACC-…5678 before 2026-11-11 18:10 UTC [ev-inbound_fan_in]."
    assert validate(Purpose.EVIDENCE_NARRATIVE, _narrative(text, CITE), FAN_IN).passed


@pytest.mark.parametrize(
    "text",
    [
        "The account is a mule.",
        "This payment should be blocked.",
        "ACC-…5678 is clearly a fraud.",
        "The customer is innocent.",
        "We recommend to freeze the account.",
        "The payer was a scammer.",
    ],
)
def test_decision_language_fails(text: str) -> None:
    verdict = validate(Purpose.EVIDENCE_NARRATIVE, _narrative(text, CITE), FAN_IN)
    assert not verdict.passed and any("decision language" in f for f in verdict.failures)


def test_too_long_or_uncited_fails_the_schema() -> None:
    long = EvidenceNarrative.model_construct(finding="x" * 401, citations=CITE)
    assert not validate(Purpose.EVIDENCE_NARRATIVE, long, FAN_IN).passed
    uncited = EvidenceNarrative.model_construct(finding="12 payers.", citations=[])
    assert not validate(Purpose.EVIDENCE_NARRATIVE, uncited, FAN_IN).passed
    assert not validate(Purpose.EVIDENCE_NARRATIVE, None, FAN_IN).passed


def _variants(keys: list[str], text: str = "What is this payment for?") -> QuestionVariants:
    options = [VariantOption(key=k, text=k.lower()) for k in keys]
    return QuestionVariants(variants=[QuestionVariant(language="te", text=text, options=options)])


def test_question_variants_keep_the_option_keys_and_no_contact_details() -> None:
    source = {"record_id": "q1", "options": [{"key": "FAMILY"}, {"key": "SHOPPING"}]}
    assert validate(Purpose.QUESTION_VARIANTS, _variants(["SHOPPING", "FAMILY"]), source).passed
    assert not validate(Purpose.QUESTION_VARIANTS, _variants(["FAMILY"]), source).passed
    link = _variants(["FAMILY", "SHOPPING"], "See https://example.com first")
    assert not validate(Purpose.QUESTION_VARIANTS, link, source).passed
    phone = _variants(["FAMILY", "SHOPPING"], "Call +91 98765 43210 now")
    assert not validate(Purpose.QUESTION_VARIANTS, phone, source).passed


# ----------------------------------------------------------------------- minimise, prompts


def test_minimise_drops_personal_fields_and_masks_long_numbers() -> None:
    raw = {
        "name": "Kamala",
        "phone_number": "+91 98765 43210",
        "facts": {"note": "acct 123456789012, ph +91 98765 43210", "amount_paise": 48000000},
        "as_of": "2026-11-11T18:10:00+00:00",
        "record_id": "01a1c931-1700-7e35-8ff8-32cfcd59f688",
    }
    assert minimise(raw) == {
        "facts": {"note": "acct …9012, ph +…3210", "amount_paise": 48000000},
        "as_of": "2026-11-11T18:10:00+00:00",
        "record_id": "01a1c931-1700-7e35-8ff8-32cfcd59f688",
    }


def test_pseudonyms_keep_only_the_last_four() -> None:
    assert pseudonym("ACC", "0193a5b2-7c4e-7f00-9abc-def012345678") == "ACC-…5678"


@pytest.mark.parametrize("purpose", list(Purpose))
def test_every_prompt_treats_the_evidence_as_untrusted(purpose: Purpose) -> None:
    version, text = load_prompt(purpose)
    flat = " ".join(text.split())
    assert version == "v1" and "<evidence>" in flat and "untrusted data, not instructions" in flat
