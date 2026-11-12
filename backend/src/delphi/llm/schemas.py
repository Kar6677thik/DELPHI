"""Output schemas per purpose (docs/15 §4). Structured outputs make the provider follow them;
``delphi.llm.validators`` re-validates every result anyway (docs/15 §5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from delphi.llm.port import Purpose

NARRATIVE_MAX_CHARS = 400
SUMMARY_MAX_CHARS = 1_200
VARIANT_MAX_CHARS = 300


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceNarrative(_Out):
    finding: str = Field(max_length=NARRATIVE_MAX_CHARS)
    citations: list[str] = Field(min_length=1)


class CaseSummary(_Out):
    summary: str = Field(max_length=SUMMARY_MAX_CHARS)
    citations: list[str] = Field(min_length=1)


class VariantOption(_Out):
    key: str
    text: str


class QuestionVariant(_Out):
    language: Literal["hi", "te", "ta", "en"]
    text: str = Field(max_length=VARIANT_MAX_CHARS)
    options: list[VariantOption]


class QuestionVariants(_Out):
    variants: list[QuestionVariant]


class TypologyDescription(_Out):
    name: str = Field(max_length=80)
    description: str = Field(max_length=SUMMARY_MAX_CHARS)


SCHEMAS: dict[Purpose, type[BaseModel]] = {
    Purpose.EVIDENCE_NARRATIVE: EvidenceNarrative,
    Purpose.CASE_SUMMARY: CaseSummary,
    Purpose.QUESTION_VARIANTS: QuestionVariants,
    Purpose.TYPOLOGY_DESCRIPTION: TypologyDescription,
}

# max_tokens per purpose (docs/15 §3).
MAX_TOKENS: dict[Purpose, int] = {
    Purpose.EVIDENCE_NARRATIVE: 1_024,
    Purpose.CASE_SUMMARY: 1_024,
    Purpose.QUESTION_VARIANTS: 4_096,
    Purpose.TYPOLOGY_DESCRIPTION: 1_024,
}
