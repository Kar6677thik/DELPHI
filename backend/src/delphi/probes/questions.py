"""Serving question probes: language, English fallback and per-customer rotation (docs/07 §9,
docs/08 §2 step 7 and §4, FR-GDN-09, FR-KB-05).

Only APPROVED variants are ever served. A PENDING_REVIEW or REJECTED variant never reaches a
customer, so unreviewed (e.g. LLM-drafted) text cannot be shown (docs/15 §1).

Selection for one customer:

1. the APPROVED variants in the customer's language; if there are none, the APPROVED English
   variants (FR-GDN-09);
2. among those, the variant shown to this customer least recently. A variant never shown to
   them counts as the least recent. Ties go to the lowest ``variant_id`` (docs/07 §9).

The caller supplies when each variant was last shown to the customer (from the customer's
earlier probe runs), so this module stays pure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from delphi.kb.model import Language, QuestionBankEntry, QuestionVariant

FALLBACK_LANGUAGE: Language = "en"
APPROVED = "APPROVED"


class NoApprovedVariantError(LookupError):
    """The probe has no APPROVED variant in the customer's language or in English."""


@dataclass(frozen=True, slots=True)
class ServedOption:
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class ServedQuestion:
    """What the customer channel shows (the ``probe.requested`` fields of docs/08 §4)."""

    probe_key: str
    variant_id: str
    language: str
    text: str
    options: tuple[ServedOption, ...]


def approved_variants(entry: QuestionBankEntry, language: str) -> tuple[QuestionVariant, ...]:
    return tuple(v for v in entry.variants if v.status == APPROVED and v.language == language)


def choose_variant(
    entry: QuestionBankEntry,
    language: str,
    last_shown: Mapping[str, datetime],
) -> QuestionVariant:
    """The variant to show this customer. ``last_shown`` maps variant_id → when it was last
    shown to them; variants missing from it were never shown."""
    candidates = approved_variants(entry, language) or approved_variants(entry, FALLBACK_LANGUAGE)
    if not candidates:
        raise NoApprovedVariantError(
            f"{entry.probe_key}: no APPROVED variant in {language!r} or {FALLBACK_LANGUAGE!r}"
        )

    def recency(variant: QuestionVariant) -> tuple[int, float, str]:
        shown = last_shown.get(variant.variant_id)
        # Never shown sorts first; then the oldest showing; then the lowest variant_id.
        if shown is None:
            return (0, 0.0, variant.variant_id)
        return (1, shown.timestamp(), variant.variant_id)

    return min(candidates, key=recency)


def serve_question(
    entry: QuestionBankEntry,
    language: str,
    last_shown: Mapping[str, datetime],
) -> ServedQuestion:
    variant = choose_variant(entry, language, last_shown)
    return ServedQuestion(
        probe_key=entry.probe_key,
        variant_id=variant.variant_id,
        language=variant.language,
        text=variant.text,
        options=tuple(ServedOption(o.key, o.text) for o in variant.options),
    )
