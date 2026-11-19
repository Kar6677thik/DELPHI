"""The evidence jobs on a real database (docs/09 §3, docs/15, #102): the narrative written for
each fetched record, and the simulated outreach call recorded through the same fetch."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from delphi.api.security import Principal
from delphi.llm.service import LlmService
from delphi.navigator.api import CaseRecord, request_evidence
from delphi.navigator.evidence import OUTREACH
from delphi.navigator.jobs import NarrativeJob
from delphi.navigator.tables import evidence_records
from delphi.platform.jobs import Job, JobContext, JobQueue, JobType, jobs
from delphi.platform.labels import labels
from delphi.platform.tables import outbox_events
from delphi.sim.generator_config import load_generator_config
from delphi.sim.jobs import OutreachCallJob
from delphi.sim.response_models import load_response_models

from .test_cases import BROWSER, _held_case, env, writer  # noqa: F401 - pytest fixtures
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
    job = NarrativeJob(env.sessions, env.clock, llm)
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


def test_a_simulated_outreach_call_is_queued_then_recorded_from_the_response_model(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env, "PROTECTIVE_HOLD")
    bad = _fetch(client, case, source_key="ACCOUNT_PROFILE", simulate=True)
    assert bad.status_code == 422
    accepted = _fetch(client, case, source_key=OUTREACH, simulate=True)
    assert accepted.status_code == 202 and accepted.json()["evidence_records"] == []
    [queued] = _jobs(env, JobType.SIM_OUTREACH_CALL)
    assert queued.params == {"case_id": str(case.id), "payment_id": str(case.subject_payment_id)}
    _ground_truth(env, case, queued.id)

    def record(case_id: UUID, outcome: str) -> None:
        with env.sessions.begin() as s:
            request_evidence(
                s, env.clock, case_id, OUTREACH, requested_by=None, recorded_outcome=outcome
            )

    job = OutreachCallJob(
        sim_engine=env.owner,
        config=load_generator_config(),
        models=load_response_models(),
        record=record,
    )
    result = job(_context(env, queued))
    assert result is not None and result["recorded"] is True
    [card] = client.get(f"/api/v1/cases/{case.id}").json()["evidence_records"]
    assert card["source_key"] == OUTREACH and card["outcome"] == result["outcome"]
    assert card["requested_by"] is None
    if result["outcome"] in {"CONFIRMS_COERCION", "DENIES_INITIATING"}:
        with env.owner.connect() as conn:
            assert (
                conn.execute(
                    select(labels.c.label_source).where(
                        labels.c.subject_id == case.subject_payment_id
                    )
                ).scalar_one()
                == "OUTREACH_CONFIRMED"
            )
    # Once recorded, another simulate request queues nothing new.
    assert _fetch(client, case, source_key=OUTREACH, simulate=True).status_code == 202
    assert len(_jobs(env, JobType.SIM_OUTREACH_CALL)) == 1


def _ground_truth(e: Env, case: CaseRecord, job_id: UUID) -> None:
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
                " stats, created_at) VALUES (:d, :j, 'test', 1, 16, 'READY', '{}'::jsonb, now())"
            ),
            {"d": uuid4(), "j": job_id},
        )
