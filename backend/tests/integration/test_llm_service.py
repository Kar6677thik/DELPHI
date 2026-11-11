"""`LlmService` on a real database (docs/15, #104): the fallback chain, the daily token budget,
the kill switch, `llm_calls`, and its 30-day redaction."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, select

from delphi.llm.port import LlmRequest, LlmResult, LlmStatus, Purpose
from delphi.llm.schemas import EvidenceNarrative
from delphi.llm.service import LlmService, NarrativeSource
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.retention import RetentionSweep
from delphi.platform.tables import llm_calls
from tests.llm_payloads import NARRATIVES

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
FAN_IN = NARRATIVES["INBOUND_FAN_IN"]
GOOD = EvidenceNarrative(
    finding="12 unrelated payers credited ACC-…5678 in the 48 h before the payment.",
    citations=["ev-inbound_fan_in"],
)


@dataclass
class FakeLlm:
    """A stand-in provider: returns ``parsed`` with ``status``, or raises ``raises``."""

    parsed: BaseModel | None = GOOD
    status: LlmStatus = LlmStatus.OK
    raises: Exception | None = None
    tokens: int = 100
    seen: list[LlmRequest] | None = None

    @property
    def name(self) -> str:
        return "fake"

    def generate(self, request: LlmRequest) -> LlmResult:
        if self.seen is not None:
            self.seen.append(request)
        if self.raises is not None:
            raise self.raises
        return LlmResult(
            self.status, self.parsed, "fake", "fake-model", self.tokens, self.tokens // 2, 7
        )


@dataclass
class Env:
    engine: Engine
    clock: FixedClock

    def service(self, primary: FakeLlm | None, budget: int = 1_000_000) -> LlmService:
        return LlmService(make_session_factory(self.engine), self.clock, primary, budget)

    def calls(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(llm_calls).order_by(llm_calls.c.id)).mappings().all()
        return [dict(r) for r in rows]


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    yield Env(engine, FixedClock(T0))
    engine.dispose()


def test_a_valid_model_answer_is_used_and_recorded(env: Env) -> None:
    seen: list[LlmRequest] = []
    out = env.service(FakeLlm(seen=seen)).generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN)
    assert out.source is NarrativeSource.LLM and out.parsed == GOOD
    [call] = env.calls()
    assert (call["provider"], call["status"], call["prompt_version"]) == ("fake", "OK", "v1")
    assert (call["input_tokens"], call["output_tokens"]) == (100, 50)
    assert call["request_redacted"]["facts"] == FAN_IN["facts"]
    assert call["validation"] == {"passed": True, "failures": []}
    assert "untrusted data" in seen[0].system  # the prompt went with the request


@pytest.mark.parametrize(
    ("provider", "status"),
    [
        (FakeLlm(EvidenceNarrative(finding="12 payers.", citations=["ev-nope"])), "INVALID_OUTPUT"),
        (
            FakeLlm(EvidenceNarrative(finding="13 payers.", citations=GOOD.citations)),
            "INVALID_OUTPUT",
        ),
        (
            FakeLlm(EvidenceNarrative(finding="ACC-…5678 is a mule.", citations=GOOD.citations)),
            "INVALID_OUTPUT",
        ),
        (FakeLlm(None, LlmStatus.REFUSED), "REFUSED"),
        (FakeLlm(raises=TimeoutError("20 s")), "ERROR"),
    ],
    ids=["bad-citation", "wrong-number", "decision-language", "refusal", "timeout"],
)
def test_anything_but_a_valid_answer_falls_back_to_the_template(
    env: Env, provider: FakeLlm, status: str
) -> None:
    out = env.service(provider).generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN)
    assert out.source is NarrativeSource.TEMPLATE
    assert isinstance(out.parsed, EvidenceNarrative) and "[ev-inbound_fan_in]" in out.parsed.finding
    first, second = env.calls()
    assert (first["provider"], first["status"]) == ("fake", status)
    assert first["validation"]["passed"] is False
    assert (second["provider"], second["status"]) == ("template", "OK")
    if status == "ERROR":
        assert first["validation"]["error"] == "TimeoutError"


def test_the_daily_budget_stops_model_calls_until_midnight(env: Env) -> None:
    service = env.service(FakeLlm(tokens=600), budget=1_000)
    assert service.generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN).source is NarrativeSource.LLM
    assert service.spent_today() == 900  # 600 in + 300 out
    assert service.generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN).source is NarrativeSource.LLM
    blocked = service.generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN)  # 1,800 ≥ 1,000
    assert blocked.source is NarrativeSource.TEMPLATE
    assert [c["status"] for c in env.calls()][-2:] == ["BUDGET_EXCEEDED", "OK"]
    env.clock.advance(timedelta(hours=12))  # past 00:00 UTC
    assert service.spent_today() == 0
    assert service.generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN).source is NarrativeSource.LLM


def test_the_kill_switch_uses_only_the_template(env: Env) -> None:
    out = env.service(None).generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN)
    assert out.source is NarrativeSource.TEMPLATE
    assert [c["provider"] for c in env.calls()] == ["template"]


def test_question_variants_without_a_model_draft_nothing(env: Env) -> None:
    out = env.service(None).generate(Purpose.QUESTION_VARIANTS, {"options": []})
    assert out.parsed is None and out.source is None
    assert [c["status"] for c in env.calls()] == ["DISABLED"]


def test_personal_fields_never_reach_the_provider_or_the_table(env: Env) -> None:
    seen: list[LlmRequest] = []
    leaky = {**FAN_IN, "name": "Kamala", "facts": {**FAN_IN["facts"], "phone": "9876543210"}}
    env.service(FakeLlm(seen=seen)).generate(Purpose.EVIDENCE_NARRATIVE, leaky)
    assert "name" not in seen[0].user_payload and "phone" not in seen[0].user_payload["facts"]
    [call] = env.calls()
    assert "name" not in call["request_redacted"]


def test_bodies_are_nulled_after_30_days_and_the_rows_kept(env: Env) -> None:
    env.service(FakeLlm()).generate(Purpose.EVIDENCE_NARRATIVE, FAN_IN)
    sessions = make_session_factory(env.engine)
    sweep = RetentionSweep(sessions, env.clock, timedelta(hours=24), timedelta(hours=24))
    assert sweep(None)["llm_call_bodies"] == 0  # type: ignore[arg-type]
    env.clock.advance(timedelta(days=31))
    assert sweep(None)["llm_call_bodies"] == 1  # type: ignore[arg-type]
    [call] = env.calls()
    assert call["request_redacted"] is None and call["response"] is None
    assert call["status"] == "OK" and call["input_tokens"] == 100  # the audit facts stay
    assert sweep(None)["llm_call_bodies"] == 0  # type: ignore[arg-type]
