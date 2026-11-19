"""The evidence jobs on a real database (docs/09 §3, docs/15, #102): the narrative written for
each fetched record, and the simulated outreach call recorded through the same fetch."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from delphi.api.app import create_app
from delphi.api.security import Principal, current_principal
from delphi.llm.service import LlmService
from delphi.navigator.api import OUTREACH, CaseRecord, request_evidence
from delphi.navigator.jobs import NarrativeJob
from delphi.navigator.tables import evidence_records
from delphi.platform.db import migration_head
from delphi.platform.jobs import Job, JobContext, JobQueue, JobType, jobs
from delphi.platform.labels import labels
from delphi.platform.settings import Settings
from delphi.platform.tables import outbox_events
from delphi.platform.wiring import Platform
from delphi.sim.external import outreach_answer
from delphi.sim.generator_config import load_generator_config
from delphi.sim.jobs import OutreachCallJob
from delphi.sim.response_models import load_response_models
from delphi.sim.scenarios import ScenarioType

from ._db import MIGRATIONS_DIR
from .test_cases import (  # noqa: F401 - pytest fixtures
    BROWSER,
    T0,
    _held_case,
    _investigator,
    env,
    writer,
)
from .test_guardian_store import Env


def _fetch(client: TestClient, case: CaseRecord, **body: Any) -> Any:
    headers = {"Idempotency-Key": str(uuid4()), **BROWSER}
    return client.post(f"/api/v1/cases/{case.id}/evidence-requests", json=body, headers=headers)


def _jobs(e: Env, job_type: JobType) -> list[Any]:
    with e.owner.connect() as conn:
        return list(conn.execute(select(jobs).where(jobs.c.type == job_type.value)))


def _context(e: Env, row: Any) -> JobContext:
    return JobContext(job=Job.from_row(row), queue=JobQueue(e.sessions, e.clock), worker_id="w1")


def test_each_fetch_queues_its_narrative_and_the_job_writes_the_template_text(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    [card] = _fetch(client, case, source_key="ACCOUNT_PROFILE").json()["evidence_records"]
    [queued] = _jobs(env, JobType.LLM_NARRATIVE)
    assert queued.params == {"evidence_record_id": card["id"]}
    llm = LlmService(env.sessions, env.clock, primary=None, daily_token_budget=0)  # templates
    job = NarrativeJob(env.sessions, llm)
    assert job(_context(env, queued)) == {"written": True, "source": "TEMPLATE"}
    [card] = client.get(f"/api/v1/cases/{case.id}").json()["evidence_records"]
    assert card["narrative_source"] == "TEMPLATE"
    # docs/15 §7: the ACCOUNT_PROFILE template, citing the record, with a pseudonymous subject.
    assert card["narrative"].startswith("ACC-…") and f"[{card['id']}]" in card["narrative"]
    assert "with full KYC" in card["narrative"]
    with env.owner.connect() as conn:
        citations = conn.execute(
            select(evidence_records.c.narrative_citations).where(
                evidence_records.c.id == UUID(card["id"])
            )
        ).scalar_one()
        ready = conn.execute(
            select(outbox_events.c.payload).where(
                outbox_events.c.type == "evidence.narrative_ready"
            )
        ).scalar_one()
    assert citations == [card["id"]] and ready["evidence_record_id"] == card["id"]
    assert job(_context(env, queued)) == {"written": False}  # written once


@pytest.fixture
def sim_writer(env: Env) -> Iterator[TestClient]:  # noqa: F811 - the imported fixture
    """An investigator's client on an api whose settings name the simulator's database (so
    simulated outreach calls can be placed)."""
    platform = Platform(
        settings=Settings(
            database_url="postgresql://unused/db",
            sim_database_url="postgresql://unused/sim",
            migrations_dir=MIGRATIONS_DIR,
        ),
        engine=env.owner,
        sessions=env.sessions,
        clock=env.clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    app = create_app(platform)
    principal = _investigator(env)
    app.dependency_overrides[current_principal] = lambda: principal
    yield TestClient(app)


def _recorder(e: Env) -> Callable[[UUID, str], None]:
    def record(case_id: UUID, outcome: str) -> None:
        with e.sessions.begin() as s:
            request_evidence(
                s, e.clock, case_id, OUTREACH, requested_by=None, recorded_outcome=outcome
            )

    return record


def _outreach_job(e: Env) -> OutreachCallJob:
    return OutreachCallJob(sim_engine=e.owner, models=MODELS, record=_recorder(e))


def _finish(e: Env, job_id: UUID) -> None:
    """What the worker does after the handler returns."""
    with e.owner.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="SUCCEEDED"))


MODELS = load_response_models()
PROFILE_SEED = load_generator_config().profiles["test"].seed


def _a_seed_that_answers_differently(payment_id: UUID) -> int:
    """A dataset seed whose outreach answer differs from the profile seed's, so the test fails
    if the job reads the profile's seed instead of the dataset's (#196 review)."""

    def answer(seed: int) -> str:
        return outreach_answer(MODELS, seed, str(payment_id), ScenarioType.APP_DIGITAL_ARREST)

    return next(s for s in range(7_000, 8_000) if answer(s) != answer(PROFILE_SEED))


EXPECTED_LABEL = {"CONFIRMS_COERCION": "APP_DIGITAL_ARREST", "DENIES_INITIATING": "ATO"}


def test_a_simulated_outreach_call_is_queued_then_recorded_from_the_dataset_seed(
    env: Env,  # noqa: F811 - the imported fixture
    sim_writer: TestClient,
) -> None:
    client = sim_writer
    case = _held_case(env, "PROTECTIVE_HOLD")
    bad = _fetch(client, case, source_key="ACCOUNT_PROFILE", simulate=True)
    assert bad.status_code == 422
    accepted = _fetch(client, case, source_key=OUTREACH, simulate=True)
    assert accepted.status_code == 202 and accepted.json()["evidence_records"] == []
    [queued] = _jobs(env, JobType.SIM_OUTREACH_CALL)
    assert queued.params == {"case_id": str(case.id), "payment_id": str(case.subject_payment_id)}
    assert case.subject_payment_id is not None
    seed = _a_seed_that_answers_differently(case.subject_payment_id)
    _ground_truth(env, case, queued.id, seed=seed)
    expected = outreach_answer(
        MODELS, seed, str(case.subject_payment_id), ScenarioType.APP_DIGITAL_ARREST
    )
    result = _outreach_job(env)(_context(env, queued))
    assert result == {"recorded": True, "outcome": expected}
    [card] = client.get(f"/api/v1/cases/{case.id}").json()["evidence_records"]
    assert card["source_key"] == OUTREACH and card["outcome"] == expected
    assert card["requested_by"] is None
    with env.owner.connect() as conn:
        found = list(
            conn.execute(
                select(labels.c.hypothesis_key).where(
                    labels.c.subject_id == case.subject_payment_id,
                    labels.c.label_source == "OUTREACH_CONFIRMED",
                )
            ).scalars()
        )
    assert found == ([EXPECTED_LABEL[expected]] if expected in EXPECTED_LABEL else [])
    # Once recorded, another simulate request queues nothing new.
    _finish(env, queued.id)
    assert _fetch(client, case, source_key=OUTREACH, simulate=True).status_code == 202
    assert len(_jobs(env, JobType.SIM_OUTREACH_CALL)) == 1


def test_a_simulated_call_needs_the_simulator_and_can_be_placed_again_after_a_miss(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
    sim_writer: TestClient,
) -> None:
    """#196 review: no 202 that never resolves."""
    case = _held_case(env, "PROTECTIVE_HOLD")
    plain, _ = writer  # an api without the simulator's database
    refused = _fetch(plain, case, source_key=OUTREACH, simulate=True)
    assert refused.status_code == 503 and refused.json()["code"] == "NOT_INITIALISED"
    assert _jobs(env, JobType.SIM_OUTREACH_CALL) == []
    assert _fetch(sim_writer, case, source_key=OUTREACH, simulate=True).status_code == 202
    assert _fetch(sim_writer, case, source_key=OUTREACH, simulate=True).status_code == 202
    [first] = _jobs(env, JobType.SIM_OUTREACH_CALL)  # still queued: nothing new
    # No ground truth for this payment: the call ends without a record.
    assert _outreach_job(env)(_context(env, first))["recorded"] is False
    _finish(env, first.id)
    assert _fetch(sim_writer, case, source_key=OUTREACH, simulate=True).status_code == 202
    assert len(_jobs(env, JobType.SIM_OUTREACH_CALL)) == 2  # a fresh attempt


def test_a_call_for_a_case_disposed_meanwhile_ends_cleanly(
    env: Env,  # noqa: F811 - the imported fixture
    sim_writer: TestClient,
) -> None:
    case = _held_case(env, "PROTECTIVE_HOLD")
    assert _fetch(sim_writer, case, source_key=OUTREACH, simulate=True).status_code == 202
    [queued] = _jobs(env, JobType.SIM_OUTREACH_CALL)
    _ground_truth(env, case, queued.id, seed=PROFILE_SEED)
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE delphi.cases SET status = 'DISPOSED', disposition = 'RELEASE',"
                " disposed_at = opened_at WHERE id = :c"
            ),
            {"c": case.id},
        )
    result = _outreach_job(env)(_context(env, queued))
    assert result is not None and result["recorded"] is False
    assert result["reason"] == "case disposed"


def _ground_truth(e: Env, case: CaseRecord, job_id: UUID, *, seed: int) -> None:
    """The payment's scenario as the generator would have stored it, and the dataset's row."""
    instance = uuid4()
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sim_truth.scenario_instances (id, scenario_type, hypothesis_key,"
                " params, started_at) VALUES (:i, 'APP_DIGITAL_ARREST', 'APP_DIGITAL_ARREST',"
                " '{}'::jsonb, now())"
            ),
            {"i": instance},
        )
        conn.execute(
            text(
                "INSERT INTO sim_truth.payment_truth (payment_id, scenario_instance_id, is_fraud,"
                " hypothesis_key, coached) VALUES (:p, :i, true, 'APP_DIGITAL_ARREST', false)"
            ),
            {"p": case.subject_payment_id, "i": instance},
        )
        conn.execute(
            text(
                "INSERT INTO delphi.datasets (id, job_id, profile, seed, generator_version, status,"
                " stats, created_at) VALUES (:d, :j, 'test', :s, 16, 'READY', '{}'::jsonb, now())"
            ),
            {"d": uuid4(), "j": job_id, "s": seed},
        )
