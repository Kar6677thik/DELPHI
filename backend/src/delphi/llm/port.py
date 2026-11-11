"""The provider port (docs/15 §2): what every adapter (Anthropic, template) implements."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel


class Purpose(StrEnum):
    """What the LLM may be used for (docs/15 §1). Nothing else."""

    EVIDENCE_NARRATIVE = "EVIDENCE_NARRATIVE"
    CASE_SUMMARY = "CASE_SUMMARY"
    QUESTION_VARIANTS = "QUESTION_VARIANTS"
    TYPOLOGY_DESCRIPTION = "TYPOLOGY_DESCRIPTION"


class LlmStatus(StrEnum):
    OK = "OK"
    REFUSED = "REFUSED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    ERROR = "ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """``user_payload`` is JSON-serialisable, already minimised (``delphi.llm.minimise``), and is
    sent inside ``<evidence>`` tags as untrusted data."""

    purpose: Purpose
    prompt_version: str
    system: str
    user_payload: Mapping[str, Any]
    output_schema: type[BaseModel]
    max_tokens: int


@dataclass(frozen=True, slots=True)
class LlmResult:
    status: LlmStatus
    parsed: BaseModel | None
    provider: str
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None  # a short reason, never the payload


class LlmPort(Protocol):
    @property
    def name(self) -> str: ...

    def generate(self, request: LlmRequest) -> LlmResult: ...
