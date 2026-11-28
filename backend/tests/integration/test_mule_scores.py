"""Mule scores in use (docs/13 §6, FR-SNS-05, #91 part 2): MULE_SWEEP fills bank.account_scores,
MuleScores serves p_mule on demand with the 0.10 fallback, and payee_p_mule reads the cache the
same way online and offline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.model import KnowledgeBase
from delphi.kb.store import ensure_kb_v1
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobQueue, JobRegistry, JobStatus, JobType, QueueConfig, enqueue
from delphi.platform.locks import MULE_SWEEP_LOCK_KEY
from delphi.platform.tables import jobs
from delphi.platform.worker import run_job
from delphi.sensing.artifacts import ArtifactKind, active_artifacts
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import build_payment_features
from delphi.sensing.history import load_payment_snapshot, payee_mule_score
from delphi.sensing.jobs import register_sensing_jobs
from delphi.sensing.mule_scores import MuleScores, MuleSweepLimits, sweep_mule_scores
from delphi.sensing.offline import OfflineHistories, load_bank_frames
from delphi.sim import api as bank
from delphi.sim.jobs import register_sim_jobs

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)  # after the `test` history
WORKER = "worker-1"
FALLBACK = load_sensing_config().features.mule_score_fallback


EVERY_ACCOUNT = MuleSweepLimits(batch=500, max_per_run=10**9)  # one run scores them all


@dataclass
class Bank:
    app: Engine
    sessions: sessionmaker[Session]
    clock: FixedClock
    queue: JobQueue
    registry: JobRegistry

    def run(self, job_type: JobType, key: str, **params: Any) -> dict[str, Any]:
        with self.sessions.begin() as s:
            enqueue(s, self.clock, job_type, idempotency_key=key, params=params)
        job = self.queue.claim(WORKER, frozenset({job_type}))
        assert job is not None
        run_job(self.queue, self.registry, job, WORKER)
        with self.sessions() as s:
            row = s.execute(select(jobs).where(jobs.c.id == job.id)).mappings().one()
        assert row["status"] == JobStatus.SUCCEEDED, row["error"]
        result: dict[str, Any] = row["result"]
        return result

    def scores(self) -> dict[str, tuple[float, datetime]]:
        s = bank.account_scores
        with self.app.connect() as conn:
            return {
                str(r.account_id): (r.p_mule, r.computed_at)
                for r in conn.execute(select(s.c.account_id, s.c.p_mule, s.c.computed_at))
            }

    def n_accounts(self) -> int:
        with self.app.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(bank.accounts)).scalar_one())


@pytest.fixture(scope="module")
def seeded(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Bank]:
    """The `test` dataset written and its models trained, as the seed does."""
    url = create_database()
    upgrade(url)
    app, sim = engine_as(url, "delphi_app"), engine_as(url, "delphi_sim")
    clock = FixedClock(T0)
    sessions = make_session_factory(app)
    with sessions.begin() as s:  # MULE_SWEEP reads mule_sweep_interval_min from the ACTIVE KB
        ensure_kb_v1(s, clock, kb)
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    register_sensing_jobs(
        registry, engine=app, sessions=sessions, clock=clock, mule_sweep=EVERY_ACCOUNT
    )
    env = Bank(app, sessions, clock, JobQueue(sessions, clock, QueueConfig()), registry)
    env.run(JobType.GENERATE_DATASET, "seed", profile="test")
    queued = env.queue.claim(WORKER, frozenset({JobType.TRAIN_SENSING}))  # queued by the seed
    assert queued is not None
    run_job(env.queue, env.registry, queued, WORKER)
    with app.connect() as conn:
        assert ArtifactKind.MULE in active_artifacts(conn)
    yield env
    app.dispose()
    sim.dispose()


# ---------------------------------------------------------------- MULE_SWEEP


def test_the_sweep_scores_every_account_and_is_idempotent(seeded: Bank) -> None:
    first = seeded.run(JobType.MULE_SWEEP, "MULE_SWEEP:slot-1")
    assert first["scored"] == seeded.n_accounts() > 0
    stored = seeded.scores()
    assert len(stored) == seeded.n_accounts()
    assert all(0.0 <= p <= 1.0 and at == T0 for p, at in stored.values())
    with seeded.app.connect() as conn:
        mule = active_artifacts(conn)[ArtifactKind.MULE]
    assert first["model_artifact_id"] == str(mule.id)
    # A retried run for the same slot rewrites the same rows with the same scores.
    seeded.run(JobType.MULE_SWEEP, "MULE_SWEEP:slot-1-retry")
    assert seeded.scores() == stored


def test_the_sweep_without_a_mule_model_scores_nothing(
    create_database: Callable[[], str],
) -> None:
    url = create_database()
    upgrade(url)
    app = engine_as(url, "delphi_app")
    try:
        result = sweep_mule_scores(app, FixedClock(T0), EVERY_ACCOUNT)
    finally:
        app.dispose()
    assert result["scored"] == 0 and result["skipped"] == "no ACTIVE MULE model"


# ---------------------------------------------------------------- on demand


def _any_account(seeded: Bank) -> UUID:
    with seeded.app.connect() as conn:
        return UUID(str(conn.execute(select(bank.accounts.c.id).limit(1)).scalar_one()))


def test_a_cached_score_is_used_only_if_computed_before_as_of(seeded: Bank) -> None:
    seeded.run(JobType.MULE_SWEEP, "MULE_SWEEP:slot-2")
    account = _any_account(seeded)
    cached, _ = seeded.scores()[str(account)]
    scores = MuleScores(seeded.app, seeded.clock)
    assert scores.p_mule(account, T0 + timedelta(minutes=1)) == cached
    # Before the sweep ran, the bank had no score: it is scored as of then, not read back.
    earlier = scores.p_mule(account, T0 - timedelta(days=3))
    assert 0.0 <= earlier <= 1.0
    assert seeded.scores()[str(account)] == (cached, T0)  # the existing row is not overwritten


def test_an_account_without_a_score_is_scored_on_demand_and_cached(seeded: Bank) -> None:
    account = _any_account(seeded)
    s = bank.account_scores
    with seeded.app.begin() as conn:
        conn.execute(s.delete().where(s.c.account_id == account))
    as_of = T0 + timedelta(hours=1)
    p = MuleScores(seeded.app, seeded.clock).p_mule(account, as_of)
    assert 0.0 <= p <= 1.0
    assert seeded.scores()[str(account)] == (p, as_of)


def test_a_failed_score_falls_back_with_a_warning(
    seeded: Bank, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    unknown = uuid4()  # no such account: the snapshot raises LookupError
    assert MuleScores(seeded.app, seeded.clock).p_mule(unknown, T0) == FALLBACK


def test_without_a_mule_model_the_fallback_is_used(create_database: Callable[[], str]) -> None:
    url = create_database()
    upgrade(url)
    app = engine_as(url, "delphi_app")
    try:
        assert MuleScores(app, FixedClock(T0)).p_mule(uuid4(), T0) == FALLBACK
    finally:
        app.dispose()


# ---------------------------------------------------------------- payee_p_mule


def test_payee_p_mule_reads_the_cache_the_same_way_online_and_offline(seeded: Bank) -> None:
    """docs/13 §3: the payee's score as of the payment. A score computed after the payment is
    not used (fallback); one computed before it is, and both paths agree (FR-SNS-02)."""
    config = load_sensing_config().features
    with seeded.app.connect() as conn:
        p = bank.payments
        payment = conn.execute(
            select(p.c.id, p.c.payee_account_id, p.c.initiated_at)
            .join(bank.accounts, bank.accounts.c.id == p.c.payer_account_id)
            .where(bank.accounts.c.customer_id.is_not(None))
            .order_by(p.c.initiated_at.desc())
            .limit(1)
        ).one()
    seeded.run(JobType.MULE_SWEEP, "MULE_SWEEP:slot-3")  # computed at T0, after the payment

    def both() -> tuple[float, float]:
        with seeded.app.connect() as conn:
            online = build_payment_features(
                *load_payment_snapshot(conn, payment.id, config), config
            )
            offline = OfflineHistories(load_bank_frames(conn), config)
            batch = build_payment_features(*offline.snapshot(str(payment.id)), config)
        return online["payee_p_mule"], batch["payee_p_mule"]

    assert both() == (FALLBACK, FALLBACK)
    s = bank.account_scores
    with seeded.app.begin() as conn:
        conn.execute(
            update(s)
            .where(s.c.account_id == payment.payee_account_id)
            .values(p_mule=0.73, computed_at=payment.initiated_at - timedelta(seconds=1))
        )
        assert (
            payee_mule_score(conn, payment.payee_account_id, payment.initiated_at, config) == 0.73
        )
    assert both() == (0.73, 0.73)


# ---------------------------------------------------------------- bounded (#252)


def test_a_run_is_capped_and_the_next_one_carries_on(seeded: Bank) -> None:
    """Scores older than mule_sweep_interval_min are stale; a run rescores at most its cap of
    them, in batches, and the next run carries on until none is left."""
    later = FixedClock(T0 + timedelta(hours=2))  # every stored score is now stale
    limits = MuleSweepLimits(batch=40, max_per_run=100)
    first = sweep_mule_scores(seeded.app, later, limits)
    assert first["scored"] == 100 and first["more"] is True
    fresh = [a for a, (_, at) in seeded.scores().items() if at == later.now()]
    assert len(fresh) == 100
    runs, total = 1, 100
    while (result := sweep_mule_scores(seeded.app, later, limits))["more"]:
        runs, total = runs + 1, total + result["scored"]
    total += result["scored"]
    assert total == seeded.n_accounts()
    assert all(at == later.now() for _, at in seeded.scores().values())
    assert sweep_mule_scores(seeded.app, later, limits) == {
        "scored": 0,
        "model_artifact_id": first["model_artifact_id"],
        "computed_at": later.now().isoformat(),
        "more": False,
    }


def test_a_run_skips_while_another_one_sweeps(seeded: Bank) -> None:
    later = FixedClock(T0 + timedelta(hours=4))
    with seeded.app.begin() as conn:
        conn.execute(select(func.pg_advisory_xact_lock(MULE_SWEEP_LOCK_KEY)))
        result = sweep_mule_scores(seeded.app, later, EVERY_ACCOUNT)
    assert result["skipped"] == "busy" and result["scored"] == 0
