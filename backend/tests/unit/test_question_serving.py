"""Question probes: language, English fallback and per-customer rotation (docs/07 §9, FR-GDN-09),
and PENDING / REJECTED variants never served (FR-KB-05)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from delphi.kb.model import KnowledgeBase, QuestionBankEntry, QuestionOption, QuestionVariant
from delphi.probes.questions import (
    NoApprovedVariantError,
    ServedOption,
    choose_variant,
    serve_question,
)

T0 = datetime(2026, 11, 9, 23, 40, tzinfo=UTC)
YES_NO = (QuestionOption(key="YES", text="Yes"), QuestionOption(key="NO", text="No"))


def _variant(variant_id: str, language: str, status: str = "APPROVED") -> QuestionVariant:
    return QuestionVariant(
        variant_id=variant_id,
        language=language,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        text=f"text of {variant_id}",
        options=YES_NO,
    )


def _entry(*variants: QuestionVariant) -> QuestionBankEntry:
    return QuestionBankEntry(probe_key="SPELL_BREAKER_DIGITAL_ARREST", variants=variants)


BANK = _entry(
    _variant("SBDA-en-1", "en"),
    _variant("SBDA-en-2", "en"),
    _variant("SBDA-en-3", "en"),
    _variant("SBDA-te-1", "te"),
    _variant("SBDA-te-2", "te"),
    _variant("SBDA-te-3", "te", status="PENDING_REVIEW"),
    _variant("SBDA-hi-1", "hi", status="PENDING_REVIEW"),
    _variant("SBDA-ta-1", "ta", status="REJECTED"),
)


# ---------------------------------------------------------------- language and fallback


def test_the_customers_language_is_used_when_it_has_an_approved_variant() -> None:
    assert choose_variant(BANK, "te", {}).variant_id == "SBDA-te-1"


@pytest.mark.parametrize("language", ["hi", "ta"])
def test_english_fallback_when_only_unapproved_variants_exist(language: str) -> None:
    # hi has only a PENDING_REVIEW variant and ta only a REJECTED one: both fall back to English
    assert choose_variant(BANK, language, {}).variant_id == "SBDA-en-1"


def test_english_customer_gets_english() -> None:
    assert choose_variant(BANK, "en", {}).language == "en"


def test_no_approved_variant_anywhere_is_an_error() -> None:
    bank = _entry(_variant("X-en-1", "en", "PENDING_REVIEW"), _variant("X-te-1", "te", "REJECTED"))
    with pytest.raises(NoApprovedVariantError, match="SPELL_BREAKER_DIGITAL_ARREST"):
        choose_variant(bank, "te", {})


# ---------------------------------------------------------------- FR-KB-05


def test_pending_and_rejected_variants_are_never_served() -> None:
    """Rotate through many showings in every language: only APPROVED variants come out."""
    unapproved = {v.variant_id for v in BANK.variants if v.status != "APPROVED"}
    for language in ("en", "hi", "te", "ta"):
        shown: dict[str, datetime] = {}
        for i in range(12):
            variant = choose_variant(BANK, language, shown)
            assert variant.status == "APPROVED"
            assert variant.variant_id not in unapproved
            shown[variant.variant_id] = T0 + timedelta(minutes=i)


def test_a_pending_variant_is_not_served_even_if_never_shown() -> None:
    shown = {"SBDA-te-1": T0, "SBDA-te-2": T0 + timedelta(minutes=1)}
    # SBDA-te-3 (PENDING_REVIEW) is the only Telugu variant never shown, but it is skipped
    assert choose_variant(BANK, "te", shown).variant_id == "SBDA-te-1"


# ---------------------------------------------------------------- rotation


def test_never_shown_variants_come_first_then_the_least_recent() -> None:
    shown = {"SBDA-en-1": T0}
    assert choose_variant(BANK, "en", shown).variant_id == "SBDA-en-2"
    shown = {
        "SBDA-en-1": T0 + timedelta(hours=2),
        "SBDA-en-2": T0,
        "SBDA-en-3": T0 + timedelta(hours=1),
    }
    assert choose_variant(BANK, "en", shown).variant_id == "SBDA-en-2"


def test_ties_go_to_the_lowest_variant_id() -> None:
    shown = {"SBDA-en-2": T0, "SBDA-en-3": T0, "SBDA-en-1": T0 + timedelta(seconds=1)}
    assert choose_variant(BANK, "en", shown).variant_id == "SBDA-en-2"


def test_rotation_cycles_through_every_approved_variant() -> None:
    shown: dict[str, datetime] = {}
    order = []
    for i in range(6):
        variant = choose_variant(BANK, "en", shown)
        order.append(variant.variant_id)
        shown[variant.variant_id] = T0 + timedelta(minutes=i)
    assert order == ["SBDA-en-1", "SBDA-en-2", "SBDA-en-3"] * 2


def test_rotation_is_per_customer() -> None:
    # Another customer's history does not affect this one: each passes its own last_shown
    other_customer = {"SBDA-en-1": T0, "SBDA-en-2": T0}
    assert choose_variant(BANK, "en", other_customer).variant_id == "SBDA-en-3"
    assert choose_variant(BANK, "en", {}).variant_id == "SBDA-en-1"


# ---------------------------------------------------------------- serving


def test_served_question_carries_the_channel_fields() -> None:
    served = serve_question(BANK, "te", {"SBDA-te-1": T0})
    assert served.probe_key == "SPELL_BREAKER_DIGITAL_ARREST"
    assert (served.variant_id, served.language, served.text) == (
        "SBDA-te-2",
        "te",
        "text of SBDA-te-2",
    )
    assert served.options == (ServedOption("YES", "Yes"), ServedOption("NO", "No"))


def test_fallback_reports_the_language_actually_shown() -> None:
    assert serve_question(BANK, "hi", {}).language == "en"


@pytest.mark.parametrize("language", ["en", "hi", "te", "ta"])
def test_every_kb_v1_question_probe_can_be_served(kb: KnowledgeBase, language: str) -> None:
    for entry in kb.question_bank.values():
        served = serve_question(entry, language, {})
        variant = next(v for v in entry.variants if v.variant_id == served.variant_id)
        assert variant.status == "APPROVED"
        assert {o.key for o in served.options} == {o.key for o in variant.options}
