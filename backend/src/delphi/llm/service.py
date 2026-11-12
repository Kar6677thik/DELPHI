"""``LlmService``: the one way features get model-written text (docs/15 §2–6).

``generate(purpose, payload)`` minimises the payload, builds the request from the current
prompt, and runs the **fallback chain**: the model provider (unless the kill switch
``DELPHI_LLM_PROVIDER=template`` is set, or today's token budget is spent), then, if it did not
return OK or its output failed validation, ``TemplateLlm``. Every attempt is one ``llm_calls``
row, with the minimised request, the output, the validation and the tokens. The result says
which source produced the text (``LLM`` or ``TEMPLATE``), so the UI labels it.

The service never decides anything: it returns text for a human to read (ADR-0004).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as clock_time
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.llm.minimise import minimise
from delphi.llm.port import LlmPort, LlmRequest, LlmResult, LlmStatus, Purpose
from delphi.llm.prompts import load_prompt
from delphi.llm.schemas import MAX_TOKENS, SCHEMAS
from delphi.llm.template import PROVIDER as TEMPLATE_PROVIDER
from delphi.llm.template import TemplateLlm
from delphi.llm.validators import Validation, validate
from delphi.platform.clock import Clock
from delphi.platform.ids import uuid7
from delphi.platform.logging import get_logger
from delphi.platform.tables import llm_calls

log = get_logger(__name__)


class NarrativeSource(StrEnum):
    """``narrative_source`` on stored text (docs/15 §2): the UI labels LLM text as AI-written."""

    LLM = "LLM"
    TEMPLATE = "TEMPLATE"


@dataclass(frozen=True, slots=True)
class Generated:
    """The text to store, or ``parsed = None`` when there is none (QUESTION_VARIANTS without
    a model: no variants are drafted)."""

    parsed: BaseModel | None
    source: NarrativeSource | None
    call_ids: tuple[UUID, ...] = field(default=())


@dataclass(slots=True)
class LlmService:
    sessions: sessionmaker[Session]
    clock: Clock
    primary: LlmPort | None  # None: the kill switch (template only)
    daily_token_budget: int
    template: TemplateLlm = field(default_factory=TemplateLlm)

    def generate(self, purpose: Purpose, payload: Mapping[str, Any]) -> Generated:
        minimised: dict[str, Any] = minimise(dict(payload))
        version, system = load_prompt(purpose)
        request = LlmRequest(
            purpose=purpose,
            prompt_version=version,
            system=system,
            user_payload=minimised,
            output_schema=SCHEMAS[purpose],
            max_tokens=MAX_TOKENS[purpose],
        )
        calls: list[UUID] = []
        if self.primary is not None:
            result, validation = self._try_primary(request)
            calls.append(self._record(request, result, validation))
            if result.status is LlmStatus.OK:
                return Generated(result.parsed, NarrativeSource.LLM, tuple(calls))
        result = self.template.generate(request)
        validation = (
            validate(purpose, result.parsed, minimised)
            if result.status is LlmStatus.OK
            else Validation(False, (result.status.value,))
        )
        if result.status is LlmStatus.OK and not validation.passed:
            # The templates are the reference output: failing here is a bug in a template or in
            # the payload a feature built. Record it and return no text rather than bad text.
            log.error("llm.template_invalid", purpose=purpose.value, failures=validation.failures)
            result = _with_status(result, LlmStatus.INVALID_OUTPUT)
        calls.append(self._record(request, result, validation))
        if result.status is LlmStatus.OK:
            return Generated(result.parsed, NarrativeSource.TEMPLATE, tuple(calls))
        return Generated(None, None, tuple(calls))

    def _try_primary(self, request: LlmRequest) -> tuple[LlmResult, Validation]:
        assert self.primary is not None
        name = self.primary.name
        if self.spent_today() >= self.daily_token_budget:
            result = LlmResult(LlmStatus.BUDGET_EXCEEDED, None, name, None, error="daily budget")
            return result, Validation(False, ("budget exceeded",))
        started = time.perf_counter()
        try:
            result = self.primary.generate(request)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            result = LlmResult(
                LlmStatus.ERROR, None, name, None, latency_ms=latency, error=type(exc).__name__
            )
        if result.status is not LlmStatus.OK:
            return result, Validation(False, (result.status.value,))
        validation = validate(request.purpose, result.parsed, request.user_payload)
        if not validation.passed:
            return _with_status(result, LlmStatus.INVALID_OUTPUT), validation
        return result, validation

    def spent_today(self) -> int:
        """Model tokens (input + output) used since 00:00 UTC; template calls cost nothing."""
        now = self.clock.now()
        midnight = datetime.combine(now.date(), clock_time(0), tzinfo=now.tzinfo)
        c = llm_calls.c
        with self.sessions() as session:
            total = session.execute(
                select(func.coalesce(func.sum(c.input_tokens + c.output_tokens), 0)).where(
                    c.created_at >= midnight, c.provider != TEMPLATE_PROVIDER
                )
            ).scalar_one()
        return int(total)

    def _record(self, request: LlmRequest, result: LlmResult, validation: Validation) -> UUID:
        now = self.clock.now()
        call_id = uuid7(unix_ms=int(now.timestamp() * 1000))
        verdict = validation.as_json()
        if result.error:
            verdict["error"] = result.error
        with self.sessions.begin() as session:
            session.execute(
                llm_calls.insert().values(
                    id=call_id,
                    purpose=request.purpose.value,
                    provider=result.provider,
                    model=result.model,
                    prompt_version=request.prompt_version,
                    request_redacted=dict(request.user_payload),
                    response=result.parsed.model_dump(mode="json") if result.parsed else None,
                    status=result.status.value,
                    validation=verdict,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                    created_at=now,
                )
            )
        return call_id


def _with_status(result: LlmResult, status: LlmStatus) -> LlmResult:
    return LlmResult(
        status,
        result.parsed,
        result.provider,
        result.model,
        result.input_tokens,
        result.output_tokens,
        result.latency_ms,
        result.error,
    )
