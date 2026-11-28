"""The demo story runner (docs/14 §6, §10, docs/21 ENG-S1/S2, #101) and the worker's Guardian
runner (#95), on a real database seeded with the `test` profile and its personas.

The prior is the docs/06 §17 fixture (sensing models are trained elsewhere); everything else is
real: the persona manifest, the story's staged payment and telemetry, the simulated passive
adapters over the bank, the simulated customer with the story's script, and the WORKER runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.api.app import create_app
from delphi.api.routers import demo as demo_router
from delphi.api.security import current_principal
from delphi.guardian.api import WorkerGuardian, live_guardian, worker_guardian
from delphi.guardian.service import GuardianService
from delphi.guardian.store import Runner, SessionMode, SessionStatus
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import ensure_kb_v1
from delphi.platform.accounts import Role
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.jobs import (
    Job,
    JobContext,
    JobQueue,
    JobRegistry,
    JobStatus,
    JobType,
    QueueConfig,
    enqueue,
)
from delphi.platform.settings import Settings
from delphi.platform.tables import jobs
from delphi.platform.wiring import Platform
from delphi.platform.worker import run_job
from delphi.probes.api import (
    SimBeneficiaryIntel,
    SimPhoneRisk,
    SimSessionTelemetry,
    passive_executor,
    passive_fetchers,
)
from delphi.sim import api as bank
from delphi.sim.catalog import datasets
from delphi.sim.customer import LiveCustomer
from delphi.sim.jobs import register_sim_jobs
from delphi.sim.persona_config import load_persona_config
from delphi.sim.phone import initiate_payment
from delphi.sim.response_models import load_response_models
from delphi.sim.stories import DemoStoryJob, _write_truth, register_story_job, stage
from delphi.sim.truth import payment_truth

from ..golden.test_story_behaviour import ARJUN_PASSIVE, KAMALA_PASSIVE
from ._db import MIGRATIONS_DIR, engine_as, upgrade
from .test_guardian_active import ARJUN_PRIOR, KAMALA_PRIOR, story_prior
from .test_guardian_phone_api import BROWSER, _signals, _until, _user, _waiting
from .test_guardian_service import OTP_KEY, Env, FixedPrior

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)  # after the `test` history ends
WORKER = "worker-1"
P_MULE = 0.1  # the payee's mule score (docs/14 §6: Kamala's payee has p_mule < 0.5)
API = "/api/v1/admin/demo/scenarios"
TIMEOUT_TOTAL_S = 60


class FixedMuleScore:
    def p_mule(self, account_id: UUID, as_of: datetime) -> float:
        return P_MULE


@dataclass
class Bank:
    url: str
    owner: Engine
    sim: Engine
    app: Engine
    sessions: sessionmaker[Session]
    clock: FixedClock
    queue: JobQueue

    def service(self, prior: FixedPrior) -> GuardianService:
        """The real simulated passive adapters, with a fixture prior and mule score."""
        timeout_ms = 2_000
        telemetry = SimSessionTelemetry(self.app, timeout_ms)
        fetchers = passive_fetchers(
            SimBeneficiaryIntel(self.app, FixedMuleScore(), timeout_ms),
            SimPhoneRisk(self.app, timeout_ms),
            telemetry,
            telemetry,
        )
        return GuardianService(
            self.sessions,
            self.clock,
            prior,
            fetchers,
            passive_executor(threads=8),
            otp_key=OTP_KEY,
            explore=False,
        )

    def env(self) -> Env:
        return Env(self.owner, self.sessions, self.clock)


@pytest.fixture
def seeded(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Bank]:
    """A migrated bank with KB v1 ACTIVE and the `test` dataset with its personas, written by
    the real GENERATE_DATASET job."""
    url = create_database()
    upgrade(url)
    owner, sim, app = engine_as(url), engine_as(url, "delphi_sim"), engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    with make_session_factory(owner).begin() as s:
        assert ensure_kb_v1(s, clock, kb) is not None
    sessions = make_session_factory(app)
    queue = JobQueue(sessions, clock, QueueConfig())
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    with sessions.begin() as s:
        enqueue(
            s,
            clock,
            JobType.GENERATE_DATASET,
            idempotency_key="seed",
            params={"profile": "test", "personas": True},
        )
    job = queue.claim(WORKER, frozenset({JobType.GENERATE_DATASET}))
    assert job is not None
    run_job(queue, registry, job, WORKER)
    yield Bank(url, owner, sim, app, sessions, clock, queue)
    for engine in (owner, sim, app):
        engine.dispose()


def _worker(seeded: Bank, kb: KnowledgeBase, prior: FixedPrior) -> tuple[WorkerGuardian, Any]:
    async def answer_after(seconds: float) -> None:  # the customer's time, on the fixed clock
        seeded.clock.advance(timedelta(seconds=seconds))

    customer = LiveCustomer(seeded.sim, load_response_models(), kb.config.classifiers)
    guardian = worker_guardian(
        seeded.service(prior), customer, seeded.sessions, seeded.clock, wait=answer_after
    )
    return guardian, customer


def _run_story(seeded: Bank, guardian: WorkerGuardian, customer: Any, **params: Any) -> Any:
    """Queue DEMO_STORY and run it as the worker does; the job row after it."""
    registry = JobRegistry()
    register_story_job(
        registry,
        sim_engine=seeded.sim,
        clock=seeded.clock,
        customer=customer,
        submit=guardian.submit,
    )
    with seeded.sessions.begin() as s:
        queued = enqueue(
            s, seeded.clock, JobType.DEMO_STORY, idempotency_key=str(uuid4()), params=params
        )
    job = seeded.queue.claim(WORKER, frozenset({JobType.DEMO_STORY}))
    assert job is not None and job.id == queued.job_id
    run_job(seeded.queue, registry, job, WORKER)
    with seeded.sessions() as s:
        return s.execute(select(jobs).where(jobs.c.id == job.id)).mappings().one()


def _decided(seeded: Bank, session_id: UUID) -> Any:
    def read() -> dict[str, Any]:
        with seeded.sessions() as s:
            row = (
                s.execute(select(guardian_sessions).where(guardian_sessions.c.id == session_id))
                .mappings()
                .one()
            )
        return dict(row)

    return _until(read, lambda r: r["status"] in ("DECIDED", "CANCELLED_BY_CUSTOMER"))


def _runs(seeded: Bank, session_id: UUID, mode: str) -> list[Any]:
    with seeded.sessions() as s:
        return list(
            s.execute(
                select(probe_runs.c.probe_key, probe_runs.c.status, probe_runs.c.outcome)
                .where(probe_runs.c.session_id == session_id, probe_runs.c.mode == mode)
                .order_by(probe_runs.c.started_at, probe_runs.c.id)
            )
        )


def test_s2_autoplay_releases_after_the_biometric(seeded: Bank, kb: KnowledgeBase) -> None:
    """ENG-S2: biometric first, never OTP, ≤ 2 active probes, RELEASE; the staged payment
    gives Arjun's golden passive outcomes (his new phone is NEW_DEVICE)."""
    guardian, customer = _worker(seeded, kb, story_prior(ARJUN_PRIOR))
    guardian.start()
    try:
        job = _run_story(seeded, guardian, customer, story="S2", mode="autoplay")
        assert job["status"] == JobStatus.SUCCEEDED, job["error"]
        result = job["result"]
        decided = _decided(seeded, UUID(result["session_id"]))
    finally:
        guardian.stop()
    assert decided["runner"] == Runner.WORKER and decided["mode"] == SessionMode.AUTOPLAY
    assert decided["decision"] == "RELEASE"
    passive = {r.probe_key: r.outcome for r in _runs(seeded, decided["id"], "PASSIVE")}
    assert passive == ARJUN_PASSIVE
    active = [r.probe_key for r in _runs(seeded, decided["id"], "ACTIVE")]
    assert active[0] == "BIOMETRIC_STEP_UP"
    assert "OTP_STEP_UP" not in active and len(active) <= 2
    with seeded.sessions() as s:
        payment = bank.get_payment(s, UUID(result["payment_id"]))
    assert payment is not None and payment.status == "EXECUTED"
    assert payment.amount_paise == 180_000 * 100 and payment.channel == "UPI_PUSH"


def test_s1_autoplay_reaches_the_protective_hold(seeded: Bank, kb: KnowledgeBase) -> None:
    """ENG-S1: purpose, then the spell-breaker (answered by the script), never OTP, and
    PROTECTIVE_HOLD; Kamala's staged payment gives her golden passive outcomes."""
    guardian, customer = _worker(seeded, kb, story_prior(KAMALA_PRIOR))
    guardian.start()
    try:
        job = _run_story(seeded, guardian, customer, story="S1", mode="autoplay")
        assert job["status"] == JobStatus.SUCCEEDED, job["error"]
        decided = _decided(seeded, UUID(job["result"]["session_id"]))
    finally:
        guardian.stop()
    passive = {r.probe_key: r.outcome for r in _runs(seeded, decided["id"], "PASSIVE")}
    assert passive == KAMALA_PASSIVE
    active = _runs(seeded, decided["id"], "ACTIVE")
    assert [r.probe_key for r in active[:2]] == ["PURPOSE_QUESTION", "SPELL_BREAKER_DIGITAL_ARREST"]
    assert [r.outcome for r in active[:2]] == [
        "PLAUSIBLE_NORMAL_TIMING",  # FAMILY_OR_FRIEND after 6 s
        "HESITATION_OR_APP_SWITCH",  # NO after 11 s with an app switch
    ]
    assert "OTP_STEP_UP" not in [r.probe_key for r in active]
    assert decided["decision"] == "PROTECTIVE_HOLD"
    with seeded.owner.connect() as conn:  # the story payment's truth (docs/14 §10)
        truth = conn.execute(
            select(payment_truth.c.hypothesis_key, payment_truth.c.coached).where(
                payment_truth.c.payment_id == decided["payment_id"]
            )
        ).one()
    assert tuple(truth) == ("APP_DIGITAL_ARREST", True)
    with seeded.owner.connect() as conn:  # the script's 23:40, for the hour features (#291)
        scripted = conn.execute(
            select(bank.payments.c.story_local_time).where(
                bank.payments.c.id == decided["payment_id"]
            )
        ).scalar_one()
    assert scripted == "23:40"


def test_the_story_job_is_idempotent(seeded: Bank, kb: KnowledgeBase) -> None:
    """A retry after a lost lease finds the payment and the session of the first attempt."""
    guardian, customer = _worker(seeded, kb, story_prior(ARJUN_PRIOR))
    handler = DemoStoryJob(
        sim_engine=seeded.sim, clock=seeded.clock, customer=customer, submit=guardian.submit
    )
    with seeded.sessions.begin() as s:
        queued = enqueue(
            s,
            seeded.clock,
            JobType.DEMO_STORY,
            idempotency_key="s2",
            params={"story": "S2", "mode": "autoplay"},
        )
    with seeded.sessions() as s:
        row = s.execute(select(jobs).where(jobs.c.id == queued.job_id)).one()
    ctx = JobContext(job=Job.from_row(row), queue=seeded.queue, worker_id=WORKER)
    guardian.start()
    try:
        first = handler(ctx)
        again = handler(ctx)  # the same job, run a second time
        assert again == first
        _decided(seeded, UUID(str(first["session_id"])))  # type: ignore[index]
        third = handler(ctx)  # also once the session has decided
    finally:
        guardian.stop()
    assert third == first
    with seeded.owner.connect() as conn:
        stories = conn.execute(
            select(func.count()).select_from(bank.payments).where(bank.payments.c.origin == "DEMO")
        ).scalar_one()
        new_phones = conn.execute(
            select(func.count())
            .select_from(bank.devices)
            .where(bank.devices.c.fingerprint.like("story-%"))
        ).scalar_one()
    assert (stories, new_phones) == (1, 1)


def test_the_worker_runner_resumes_its_sessions_at_start(seeded: Bank, kb: KnowledgeBase) -> None:
    """FR-GDN-14: a WORKER session the last worker left unfinished is driven at start-up; the
    simulated customer answers it from the story's truth (its script died with the worker)."""
    config = load_persona_config()
    service = seeded.service(story_prior(ARJUN_PRIOR))
    payment_id = uuid4()
    at = seeded.clock.now()
    with seeded.sim.begin() as conn:
        staged = stage(conn, config, "S2", at)
        initiate_payment(conn, staged.request, at, payment_id=payment_id)
        _write_truth(conn, config.stories["S2"], "S2", payment_id, at)
    started = service.start(payment_id, mode=SessionMode.AUTOPLAY, runner=Runner.WORKER)
    asyncio.run(service.passive(started.id))  # then the old worker died
    guardian, _ = _worker(seeded, kb, story_prior(ARJUN_PRIOR))
    try:
        assert guardian.start() == [started.id]
        decided = _decided(seeded, started.id)
    finally:
        guardian.stop()
        guardian.stop()  # a second stop is harmless
    assert decided["status"] == SessionStatus.DECIDED
    assert decided["runner"] == Runner.WORKER


# ---------------------------------------------------------------- the endpoint


def _platform(seeded: Bank, **settings: Any) -> Platform:
    return Platform(
        settings=Settings(
            database_url="postgresql://unused/db", migrations_dir=MIGRATIONS_DIR, **settings
        ),
        engine=seeded.owner,
        sessions=seeded.sessions,
        clock=seeded.clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )


def _client(seeded: Bank, role: Role, *, live: Any = None, **settings: Any) -> TestClient:
    app = create_app(_platform(seeded, **settings), guardian_runner=live)
    principal = _user(seeded.env(), role)
    app.dependency_overrides[current_principal] = lambda: principal
    return TestClient(app, headers=BROWSER)


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


@pytest.fixture
def initialised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dataset is seeded; the sensing models are trained elsewhere (docs/13 §7)."""
    monkeypatch.setattr(demo_router, "require_initialised", lambda _request: None)


def test_the_endpoint_needs_demo_drive_and_a_runnable_story(seeded: Bank) -> None:
    with _client(seeded, Role.VIEWER, simulation_enabled=True) as viewer:
        refused = viewer.post(f"{API}/S2/run", json={"mode": "autoplay"}, headers=_key())
        assert refused.status_code == 403
    with _client(seeded, Role.INVESTIGATOR, simulation_enabled=True) as client:
        for story in ("S3", "S4"):
            later = client.post(f"{API}/{story}/run", json={"mode": "autoplay"}, headers=_key())
            assert later.status_code == 422 and later.json()["code"] == "VALIDATION_FAILED"
            assert "cannot be run yet" in later.json()["detail"]
        assert (
            client.post(f"{API}/S9/run", json={"mode": "autoplay"}, headers=_key()).status_code
            == 422
        )
        bad_mode = client.post(f"{API}/S1/run", json={"mode": "replay"}, headers=_key())
        assert bad_mode.status_code == 422
        # the models are not trained in this bank: not initialised yet
        early = client.post(f"{API}/S1/run", json={"mode": "autoplay"}, headers=_key())
        assert early.status_code == 503 and early.json()["code"] == "NOT_INITIALISED"


def test_the_endpoint_needs_the_simulation_and_for_interactive_a_runner(
    seeded: Bank, initialised: None
) -> None:
    with _client(seeded, Role.INVESTIGATOR) as client:  # DELPHI_SIMULATION_ENABLED off
        off = client.post(f"{API}/S2/run", json={"mode": "autoplay"}, headers=_key())
        assert off.status_code == 503 and "simulation is off" in off.json()["detail"]
    with _client(seeded, Role.INVESTIGATOR, simulation_enabled=True) as client:
        no_runner = client.post(f"{API}/S2/run", json={"mode": "interactive"}, headers=_key())
        assert no_runner.status_code == 503 and no_runner.json()["code"] == "NOT_INITIALISED"


def test_the_endpoint_queues_one_story_job_per_key(seeded: Bank, initialised: None) -> None:
    with _client(seeded, Role.INVESTIGATOR, simulation_enabled=True) as client:
        key = _key()
        queued = client.post(f"{API}/S2/run", json={"mode": "autoplay"}, headers=key)
        assert queued.status_code == 202, queued.text
        job = queued.json()
        assert (job["type"], job["status"]) == ("DEMO_STORY", "QUEUED")
        replay = client.post(f"{API}/S2/run", json={"mode": "autoplay"}, headers=key)
        assert replay.json()["id"] == job["id"]
    with seeded.sessions() as s:
        params = s.execute(select(jobs.c.params).where(jobs.c.id == UUID(job["id"]))).scalar_one()
    assert params == {"story": "S2", "mode": "autoplay"}


def test_interactive_mode_gives_the_phone_a_payment_its_runner_drives(
    seeded: Bank, kb: KnowledgeBase, initialised: None
) -> None:
    """The job resolves Arjun's story payment (and stages his new phone); the presenter's phone
    pays it, so the session runs on the api's runner (runner API) and the phone answers it."""
    guardian, customer = _worker(seeded, kb, story_prior(ARJUN_PRIOR))
    job = _run_story(seeded, guardian, customer, story="S2", mode="interactive")  # no runner
    assert job["status"] == JobStatus.SUCCEEDED, job["error"]
    result = job["result"]
    payment = result["payment"]
    assert payment["amount_paise"] == 180_000 * 100 and payment["telemetry"]["on_call"] is False
    assert "story_local_time" not in payment  # S-2 has no scripted time
    with seeded.sessions() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(bank.payments)
                .where(bank.payments.c.origin == "DEMO")
            ).scalar_one()
            == 0
        )  # the phone makes it
    live = live_guardian(seeded.service(story_prior(ARJUN_PRIOR)), seeded.sessions, seeded.clock)
    with _client(seeded, Role.INVESTIGATOR, live=live, simulation_enabled=True) as client:
        started = client.post(
            "/api/v1/guardian/sessions", json={"payment": payment}, headers=_key()
        )
        assert started.status_code == 201, started.text
        sid = started.json()["id"]

        def read() -> dict[str, Any]:
            return dict(client.get(f"/api/v1/guardian/sessions/{sid}").json())

        view = _until(read, lambda v: v["status"] in ("AWAITING_CUSTOMER", "DECIDED"))
        run = _waiting(view)
        assert run is not None and run["probe_key"] == "BIOMETRIC_STEP_UP"
        answered = client.post(
            f"/api/v1/guardian/sessions/{sid}/probe-responses",
            json={
                "probe_run_id": run["id"],
                "answer": {"biometric": "MATCH"},
                "client_signals": _signals(seeded.env(), 2.0),
            },
            headers=_key(),
        )
        assert answered.status_code == 200, answered.text
    with seeded.sessions() as s:
        runner = s.execute(
            select(guardian_sessions.c.runner).where(guardian_sessions.c.id == UUID(sid))
        ).scalar_one()
    assert runner == Runner.API.value
    assert started.json()["payment"]["device_id"] == payment["device_id"]


def test_a_story_needs_a_dataset_with_personas(owner_engine: Engine, kb: KnowledgeBase) -> None:
    """An empty bank, or one seeded without personas, cannot stage a story (the job fails with
    the reason), and the simulated customer has no dataset to answer from."""
    config = load_persona_config()
    with owner_engine.connect() as conn, pytest.raises(LookupError, match="no dataset"):
        stage(conn, config, "S1", T0)
    customer = LiveCustomer(owner_engine, load_response_models(), kb.config.classifiers)
    with pytest.raises(LookupError, match="no dataset"):
        customer.answer(
            payment_id=uuid4(),
            probe_key="PURPOSE_QUESTION",
            options=[],
            otp_code=None,
            timeout_s=60,
            at=T0,
        )
    with Session(owner_engine) as session:  # rolled back: the shared database stays empty
        seed_job = enqueue(session, FixedClock(T0), JobType.GENERATE_DATASET, idempotency_key="x")
        session.execute(
            datasets.insert().values(
                id=uuid4(),
                job_id=seed_job.job_id,
                profile="test",
                seed=1,
                generator_version=1,
                status="READY",
                stats={},
                seed_completed_at=T0,
                created_at=T0,
            )
        )
        with pytest.raises(LookupError, match="has no personas"):
            stage(session.connection(), config, "S1", T0)
        session.rollback()
