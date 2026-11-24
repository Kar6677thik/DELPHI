"""The online sensing inference (docs/13 §1, §7, #92): the Guardian's PriorPort on the `test`
dataset with its trained models, NOT_INITIALISED without them, the reload on activation, and
the p95 ≤ 20 ms latency (NFR-03)."""

from __future__ import annotations

import dataclasses
import math
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.jobs import JobQueue, JobRegistry, JobType, QueueConfig, enqueue
from delphi.platform.worker import run_job
from delphi.sensing.api import MuleScores, PriorInputs, SensingInference
from delphi.sensing.artifacts import (
    ArtifactKind,
    TrainedModel,
    active_artifacts,
    load_blob,
    model_artifacts,
    store_artifact,
)
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import build_payment_features
from delphi.sensing.jobs import register_sensing_jobs
from delphi.sensing.mule_scores import sweep_mule_scores
from delphi.sensing.offline import OfflineHistories, load_bank_frames
from delphi.sensing.prior import PriorModel
from delphi.sim import api as bank
from delphi.sim.jobs import register_sim_jobs

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)  # after the `test` history
WORKER = "worker-1"
SETTINGS = load_sensing_config()
TARGET_P95_MS = 20.0  # docs/13 §7, NFR-03
BENCH_PAYMENTS = 60
WARM_UP = 10


@dataclass
class Bank:
    app: Engine
    sessions: sessionmaker[Session]
    clock: FixedClock


@pytest.fixture(scope="module")
def seeded(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Bank]:
    """The `test` dataset written and its models trained, as the seed does."""
    url = create_database()
    upgrade(url)
    app, sim = engine_as(url, "delphi_app"), engine_as(url, "delphi_sim")
    clock = FixedClock(T0)
    sessions = make_session_factory(app)
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    register_sensing_jobs(registry, engine=app, sessions=sessions, clock=clock)
    queue = JobQueue(sessions, clock, QueueConfig())
    with sessions.begin() as s:
        enqueue(
            s, clock, JobType.GENERATE_DATASET, idempotency_key="seed", params={"profile": "test"}
        )
    for job_type in (JobType.GENERATE_DATASET, JobType.TRAIN_SENSING):  # the seed queues training
        job = queue.claim(WORKER, frozenset({job_type}))
        assert job is not None
        run_job(queue, registry, job, WORKER)
    with app.connect() as conn:
        assert set(active_artifacts(conn)) == set(ArtifactKind)
    yield Bank(app, sessions, clock)
    app.dispose()
    sim.dispose()


def _customer_payments(seeded: Bank, n: int, seed: int) -> list[bank.PaymentView]:
    with seeded.app.connect() as conn:
        p, a = bank.payments, bank.accounts
        ids = conn.execute(
            select(p.c.id)
            .join(a, a.c.id == p.c.payer_account_id)
            .where(a.c.customer_id.is_not(None))
            .order_by(p.c.id)
        ).scalars()
        chosen = random.Random(seed).sample(list(ids), n)
        views = [bank.get_payment(conn, i) for i in chosen]
    assert all(v is not None for v in views)
    return [v for v in views if v is not None]


def _active_prior(seeded: Bank) -> PriorModel:
    with seeded.app.connect() as conn:
        artifact = active_artifacts(conn)[ArtifactKind.PRIOR]
        return PriorModel.from_bytes(load_blob(conn, artifact.id))


def test_prior_inputs_match_the_trained_models_on_offline_features(seeded: Bank) -> None:
    """The online path gives what the training-side features and the stored models give."""
    service = SensingInference(seeded.app, seeded.clock)
    assert set(service.refresh()) == set(ArtifactKind)
    prior = _active_prior(seeded)
    with seeded.app.connect() as conn:
        offline = OfflineHistories(load_bank_frames(conn), SETTINGS.features)
    for payment in _customer_payments(seeded, 20, seed=92):
        inputs = service.prior_inputs(payment)
        assert isinstance(inputs, PriorInputs)
        assert list(inputs.p_payment) == list(SETTINGS.prior.classes)
        assert math.isclose(sum(inputs.p_payment.values()), 1.0, abs_tol=1e-9)
        assert 0.0 <= inputs.anomaly_percentile <= 1.0
        assert inputs.p_mule_payer is not None and 0.0 <= inputs.p_mule_payer <= 1.0
        assert len(inputs.top_features) == SETTINGS.prior.top_contributions
        for entry in inputs.top_features:
            assert set(entry) == {"feature", "value", "contribution"}
            assert entry["feature"] in prior.feature_list
        features = build_payment_features(*offline.snapshot(str(payment.id)), SETTINGS.features)
        expected = prior.predict({f: features[f] for f in prior.feature_list})
        for h, p in expected.items():
            assert inputs.p_payment[h] == pytest.approx(p, abs=1e-9)


def test_p_mule_serves_the_mule_score(seeded: Bank) -> None:
    payment = _customer_payments(seeded, 1, seed=7)[0]
    service = SensingInference(seeded.app, seeded.clock)
    expected = MuleScores(seeded.app, seeded.clock).p_mule(
        payment.payee_account_id, payment.initiated_at
    )
    assert service.p_mule(payment.payee_account_id, payment.initiated_at) == expected
    assert 0.0 <= service.p_mule(payment.payee_account_id) <= 1.0  # as of the clock


def test_a_payment_not_made_by_a_customer_is_not_found(seeded: Bank) -> None:
    with seeded.app.connect() as conn:
        p, a = bank.payments, bank.accounts
        payment_id = conn.execute(
            select(p.c.id)
            .join(a, a.c.id == p.c.payer_account_id)
            .where(a.c.customer_id.is_(None))
            .limit(1)
        ).scalar_one()
        view = bank.get_payment(conn, payment_id)
    assert view is not None
    with pytest.raises(NotFoundError):
        SensingInference(seeded.app, seeded.clock).prior_inputs(view)


@pytest.mark.benchmark
def test_inference_p95(seeded: Bank) -> None:
    """NFR-03: p95 ≤ 20 ms per payment. Warn-level in CI (docs/21 §1): the assertion allows 3×
    headroom for noisy shared runners, like the planner benchmark, and prints the figure.

    As in live use, the payers' mule scores come from a MULE_SWEEP run before the payments
    (docs/13 §6); scoring an account on demand is the fallback for one the sweep missed."""
    payments = _customer_payments(seeded, BENCH_PAYMENTS, seed=20)
    earliest = min(p.initiated_at for p in payments)
    sweep_mule_scores(seeded.app, FixedClock(earliest - timedelta(seconds=1)))
    service = SensingInference(seeded.app, seeded.clock)
    service.refresh()
    timings = []
    for payment in payments:
        start = time.perf_counter()
        service.prior_inputs(payment)
        timings.append((time.perf_counter() - start) * 1000)
    p95 = float(np.percentile(timings[WARM_UP:], 95))
    print(f"\nsensing inference p95 = {p95:.1f} ms (target {TARGET_P95_MS} ms)")
    assert p95 <= 3 * TARGET_P95_MS


def test_a_newly_activated_artifact_is_served_without_a_restart(seeded: Bank) -> None:
    payment = _customer_payments(seeded, 1, seed=3)[0]
    service = SensingInference(seeded.app, seeded.clock)
    before = service.prior_inputs(payment)
    current = _active_prior(seeded)
    hotter = dataclasses.replace(current, temperature=current.temperature * 4)
    with seeded.sessions.begin() as s:
        stored = store_artifact(
            s,
            seeded.clock,
            TrainedModel(
                kind=ArtifactKind.PRIOR,
                blob=hotter.to_bytes(),
                metrics={},
                feature_list=hotter.feature_list,
                training_cutoff=T0,
                gate_passed=True,
            ),
            dataset_id=None,
            job_id=None,
        )
    after = service.prior_inputs(payment)
    assert service.refresh()[ArtifactKind.PRIOR] == stored.id
    assert after.p_payment != before.p_payment
    assert after.anomaly_percentile == before.anomaly_percentile
    with seeded.app.connect() as conn:
        offline = OfflineHistories(load_bank_frames(conn), SETTINGS.features)
    features = build_payment_features(*offline.snapshot(str(payment.id)), SETTINGS.features)
    expected = hotter.predict({f: features[f] for f in hotter.feature_list})
    assert after.p_payment == pytest.approx(expected, abs=1e-9)


def test_without_a_mule_model_the_payer_score_is_left_out(seeded: Bank) -> None:
    payment = _customer_payments(seeded, 1, seed=5)[0]
    a = model_artifacts.c
    with seeded.app.begin() as conn:
        conn.execute(
            update(model_artifacts)
            .where(a.kind == ArtifactKind.MULE.value, a.status == "ACTIVE")
            .values(status="RETIRED")
        )
    assert SensingInference(seeded.app, seeded.clock).prior_inputs(payment).p_mule_payer is None


def test_without_active_models_inference_is_not_initialised(
    create_database: Callable[[], str],
) -> None:
    url = create_database()
    upgrade(url)
    app = engine_as(url, "delphi_app")
    payment = bank.PaymentView(
        id=uuid4(),
        payer_account_id=uuid4(),
        payee_account_id=uuid4(),
        payer_customer_id=None,
        amount_paise=100_000,
        channel="IMPS",
        initiated_at=T0,
        device_id=None,
        status="PENDING_DECISION",
    )
    service = SensingInference(app, FixedClock(T0))
    try:
        assert service.refresh() == {}
        with pytest.raises(DelphiError) as raised:
            service.prior_inputs(payment)
    finally:
        app.dispose()
    assert raised.value.code == ProblemCode.NOT_INITIALISED
    assert "PRIOR" in str(raised.value) and "ANOMALY" in str(raised.value)
