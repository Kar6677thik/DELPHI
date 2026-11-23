"""The case DD pack (docs/09 §7, FR-NAV-11, NFR-07, #108) on story S-1's path.

S-1 (docs/14 §6): a coached digital-arrest victim pays ₹4,80,000; Guardian's probes see the
scam and take a PROTECTIVE_HOLD, which opens a COERCION case. The investigator fetches
evidence, the narratives are written and the case is decided: its pack must then be complete,
including the observations it inherited from the Guardian session.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from delphi.guardian.api import case_trail
from delphi.guardian.service import Decided
from delphi.llm.service import LlmService
from delphi.navigator.api import (
    OUTREACH,
    Disposition,
    build_case_dd_pack,
    dispose,
    get_case,
    refresh_dd_pack_complete,
    request_evidence,
)
from delphi.navigator.jobs import NarrativeJob
from delphi.platform.accounts import Role, create_user
from delphi.platform.audit import ActorType
from delphi.platform.ddpack import render_html
from delphi.platform.jobs import Job, JobContext, JobQueue, JobType, jobs

from .test_guardian_service import DIGITAL_ARREST, SCAM, Env, FixedPrior, _run, make_env

S1_AMOUNT_INR = 480_000


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    yield from make_env(create_database)


def _s1_case(env: Env) -> UUID:
    """S-1 through Guardian: the protective hold opens the coercion case."""
    payment = env.payment(S1_AMOUNT_INR)
    result = _run(env, env.service(FixedPrior(DIGITAL_ARREST), SCAM), payment)
    assert isinstance(result, Decided) and result.session.decision == "PROTECTIVE_HOLD"
    assert result.session.case_id is not None
    return result.session.case_id


def _investigator(env: Env) -> UUID:
    with env.sessions.begin() as s:
        return create_user(
            s,
            env.clock,
            username=f"inv-{uuid4().hex[:8]}",
            display_name="Investigator",
            password="correct horse battery staple",
            role=Role.INVESTIGATOR,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        ).id


def _pack(env: Env, case_id: UUID) -> Any:
    with env.sessions() as s:
        case = get_case(s, case_id)
        assert case is not None and case.guardian_session_id is not None
        return build_case_dd_pack(s, case_id, case_trail(s, case.guardian_session_id))


def _sections(pack: Any) -> dict[int, Any]:
    return {s.number: s for s in pack.sections}


def _write_narratives(env: Env) -> None:
    """Run every queued LLM_NARRATIVE job with the templates (no model), as the worker would."""
    llm = LlmService(env.sessions, env.clock, primary=None, daily_token_budget=0)
    job = NarrativeJob(env.sessions, llm, case_trail)
    with env.owner.connect() as conn:
        queued = list(conn.execute(select(jobs).where(jobs.c.type == JobType.LLM_NARRATIVE.value)))
    for row in queued:
        ctx = JobContext(
            job=Job.from_row(row), queue=JobQueue(env.sessions, env.clock), worker_id="w"
        )
        assert job(ctx) == {"written": True, "source": "TEMPLATE"}


def _decide(env: Env, case_id: UUID, user: UUID) -> None:
    with env.sessions.begin() as s:
        case = get_case(s, case_id)
        assert case is not None
        dispose(
            s,
            env.clock,
            case_id,
            Disposition(
                decision="CONFIRM_FRAUD",
                confidence=4,
                typology="APP_DIGITAL_ARREST",
                note=None,
                override_rationale=(
                    None
                    if case.recommended_decision in (None, "CONFIRM_FRAUD")
                    else "The customer confirmed the caller coached them on the call."
                ),
            ),
            expected_version=case.version,
            user_id=user,
        )
        refresh_dd_pack_complete(s, case_id, case_trail)


def test_an_open_s1_case_inherits_the_session_but_is_never_complete(env: Env) -> None:
    case_id = _s1_case(env)
    pack = _pack(env, case_id)
    sections = _sections(pack)
    assert not pack.complete
    assert sections[5].content is None  # docs/09 §7: section 5 needs the human disposition
    steps = sections[3].content
    assert steps and all(s["origin"] == "GUARDIAN" for s in steps)  # the session's observations
    assert sections[4].required and sections[4].filled  # guardrails for Guardian observations
    assert sections[2].filled and sections[2].content["top_features"] is not None
    assert sections[1].content["protocol"] == "COERCION"
    assert sections[1].content["amount_paise"] == S1_AMOUNT_INR * 100
    assert not sections[7].required  # no chase before M6


def test_a_decided_s1_case_has_a_complete_pack_once_its_narratives_are_written(
    env: Env,
) -> None:
    """The acceptance criterion: 100% of S-1 cases end with a complete DD pack (NFR-07)."""
    case_id = _s1_case(env)
    user = _investigator(env)
    with env.sessions.begin() as s:
        request_evidence(s, env.clock, case_id, "ACCOUNT_PROFILE", requested_by=user)
        request_evidence(
            s,
            env.clock,
            case_id,
            OUTREACH,
            requested_by=user,
            recorded_outcome="CONFIRMS_COERCION",
            note="Caller claimed to be the police; customer was told to stay on the line.",
        )
    _decide(env, case_id, user)

    # Decided, but the narratives are still being written: section 6 is incomplete.
    pack = _pack(env, case_id)
    assert not pack.complete
    assert "Waiting for narratives" in (_sections(pack)[6].note or "")
    with env.sessions() as s:
        case = get_case(s, case_id)
    assert case is not None and not case.dd_pack_complete

    _write_narratives(env)  # each job recomputes the flag (docs/09 §7: on every change)

    pack = _pack(env, case_id)
    sections = _sections(pack)
    assert pack.complete
    assert all(s.filled for s in pack.sections if s.required)
    with env.sessions() as s:
        case = get_case(s, case_id)
    assert case is not None and case.dd_pack_complete  # stored, as the metric reads it
    origins = [step["origin"] for step in sections[3].content]
    assert origins[-2:] == ["NAVIGATOR", "NAVIGATOR"] and "GUARDIAN" in origins
    human = sections[5].content["human"]
    assert (human["decision"], human["typology"], human["confidence"]) == (
        "CONFIRM_FRAUD",
        "APP_DIGITAL_ARREST",
        4,
    )
    narratives = sections[6].content
    assert [n["label"] for n in narratives] == ["Template summary", "Template summary"]
    assert all(c["source"] for n in narratives for c in n["citations"])  # resolved citations
    audit = sections[8].content
    assert audit["case"]["ok"] and audit["guardian_session"]["ok"]
    assert "Due-diligence pack: Case" in render_html(pack)


def test_a_case_decided_without_evidence_says_so_and_can_be_complete(env: Env) -> None:
    case_id = _s1_case(env)
    _decide(env, case_id, _investigator(env))
    sections = _sections(_pack(env, case_id))
    assert sections[6].content == "No evidence was fetched for this case."
    assert sections[6].filled


def test_a_failing_check_never_blocks_the_change(env: Env) -> None:
    case_id = _s1_case(env)

    def broken(_session: Any, _session_id: UUID) -> Any:
        raise RuntimeError("the session's pack could not be read")

    with env.sessions.begin() as s:
        assert refresh_dd_pack_complete(s, case_id, broken) is False
        assert get_case(s, case_id) is not None  # the transaction is still usable
