"""S-4 calibration of the prize advance-fee scenario (docs/14 §5.4, docs/21 §3 ENG-S4; #36, v18).

Under KB v1, at least 70% of PRIZE_ADVANCE_FEE sessions must end with P(UNEXPLAINED) at or above
the Scout routing threshold (0.35). Each session's prior comes from sensing models trained here on
the `demo` profile, as docs/14 §5.4 says, by the same code as ``TRAIN_SENSING``: the prior model
(docs/13 §4) on the labels the seed delivers, and the anomaly forest (§5) on the prior's GENUINE
training rows. Each S-4 victim's payment is inserted into the bank and read back with the live
``load_payment_snapshot``, as the Guardian scores it, then rolled back.

Training on `demo` takes several minutes, so the check is `slow` (nightly, owner decision on #36).
PR CI runs the same pipeline on the `test` prior as a smoke check that asserts only that it runs.
If the check fails, tune only the scenario's telemetry and answers in ``sim/config`` (never the
KB) and record it in the generator changelog (docs/14 §5.4).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import numpy as np
import pytest
from sqlalchemy import Engine, insert, select

from delphi.engine import (
    CompiledProfile,
    Phase,
    SessionFacts,
    build_prior,
    initial_budgets,
    plan_next,
    update,
)
from delphi.kb.model import GENUINE, UNEXPLAINED, KnowledgeBase
from delphi.sensing.anomaly import AnomalyModel, train_anomaly_matrix
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import build_payment_features
from delphi.sensing.history import load_payment_snapshot
from delphi.sensing.prior import PriorModel, train_prior
from delphi.sensing.trainers import load_prior_data
from delphi.sim import api as bank
from delphi.sim.api import DatasetInfo
from delphi.sim.dataset import generate_dataset
from delphi.sim.delivery import deliver_labels
from delphi.sim.generator_config import load_generator_config
from delphi.sim.response_models import (
    ResponseModels,
    SignalDraw,
    draw_signals,
    load_response_models,
)
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType
from delphi.sim.writer import write_dataset

from ..integration._db import engine_as, upgrade
from ..integration.conftest import admin_url, create_database  # noqa: F401 - fixtures

PRIZE = ScenarioType.PRIZE_ADVANCE_FEE
SESSIONS = 15  # docs/14 §6: 15 S-4 victims on `demo`
REQUIRED_SHARE = 0.70
SEED = 20261031  # the `demo` profile seed (docs/14 §2)
DEMO_CREATED_AT = datetime(2026, 11, 1, tzinfo=UTC)  # a fixed `demo` seed time, for determinism

# (p_model, anomaly percentile q) for one session.
PriorInputs = tuple[Mapping[str, float], float]
PriorFn = Callable[[np.random.Generator, float], PriorInputs]


def _scenario() -> tuple[tuple[float, float], str]:
    """The scenario's amount range (₹) and channel, from the generator config (docs/14 §4)."""
    cfg = load_generator_config().prize_advance_fee
    return (cfg.amount_inr.min, cfg.amount_inr.max), cfg.channel


def _passive_outcomes(signals: SignalDraw) -> dict[str, str]:
    """The telemetry-driven passive probes, by the first-match rules of docs/08 §5."""
    if signals.paste_or_long_dwell:
        behaviour = "HESITANT_OR_PASTED"
    elif signals.cadence_mismatch:
        behaviour = "CADENCE_MISMATCH"
    else:
        behaviour = "NORMAL"
    return {
        "DEVICE_POSTURE": "REMOTE_ACCESS_ACTIVE"
        if signals.remote_access_active
        else "KNOWN_DEVICE",
        "SESSION_BEHAVIOUR": behaviour,
        "CALL_STATUS": (
            "ON_CALL_UNKNOWN_NUMBER" if signals.on_call_unknown_number else "NOT_ON_CALL"
        ),
    }


def run_prize_advance_fee_sessions(
    profile: CompiledProfile, models: ResponseModels, prior_fn: PriorFn
) -> list[float]:
    """Terminal P(UNEXPLAINED) of each S-4 session: passive phase, then planned probes answered
    from the frozen prize advance-fee response model."""
    (low, high), channel = _scenario()
    rng = np.random.Generator(np.random.PCG64(SEED))
    unexplained = profile.hypotheses.index(UNEXPLAINED)
    facts = SessionFacts(channel=channel, consents=frozenset({"call_status"}))
    results = []
    for _ in range(SESSIONS):
        amount_inr = math.exp(rng.uniform(math.log(low), math.log(high)))
        p_model, q = prior_fn(rng, amount_inr)
        belief = build_prior(profile, p_model, q)
        observed: set[str] = set()
        passive = _passive_outcomes(draw_signals(rng, models.telemetry_targets[PRIZE]))
        for key, outcome in passive.items():
            src = profile.sources[key]
            belief = update(belief, src.likelihood[src.outcomes.index(outcome)])
            observed.add(key)
        budgets = initial_budgets(profile)
        while True:
            plan = plan_next(
                profile,
                belief,
                amount_paise=round(amount_inr * 100),
                vulnerable=False,
                phase=Phase.GUARDIAN_ACTIVE,
                observed=frozenset(observed),
                facts=facts,
                budgets=budgets,
            )
            if plan.is_stop:
                break
            src = profile.sources[plan.action]
            answer = models.sample_answer(rng, PRIZE, plan.action)
            belief = update(belief, src.likelihood[src.outcomes.index(answer)])
            observed.add(plan.action)
            budgets = budgets.after(src)
        results.append(float(belief[unexplained]))
    return results


@dataclass(frozen=True)
class Sensing:
    """Sensing models trained on one profile's seeded bank, and what an S-4 payment needs."""

    engine: Engine  # the seeded bank (owner role), to insert and read back each payment
    prior: PriorModel
    anomaly: AnomalyModel
    anomaly_features: tuple[str, ...]
    victims: tuple[UUID, ...]  # the S-4 victims' accounts, in persona order
    payee: UUID  # a never-listed layer-1 mule (CLEAN_UNKNOWN), as the generator picks
    start: datetime  # the live window opens when the history ends


def _train_sensing(create_db: Callable[[], str], kb: KnowledgeBase, profile: str) -> Sensing:
    url = create_db()
    upgrade(url)
    settings = load_sensing_config()
    created_at = DEMO_CREATED_AT if profile == "demo" else None
    config = load_generator_config()
    dataset = generate_dataset(config, profile, kb=kb, personas=True, created_at=created_at)
    assert dataset.personas is not None
    window = bank.history_window(DatasetInfo(uuid4(), profile, 1, created_at or datetime.now(UTC)))
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:  # the bank as the seed leaves it: the history's labels delivered
        write_dataset(conn, dataset)
        deliver_labels(
            conn, until=window.end, active_hypotheses=frozenset(kb.hypotheses), now=window.end
        )
    sim.dispose()
    app = engine_as(url, "delphi_app")
    with app.connect() as conn:
        data = load_prior_data(conn, window, settings)
    app.dispose()
    prior, _gate = train_prior(
        data.train, data.validation, data.test, data.feature_list, settings.prior, profile=profile
    )
    # docs/13 §5, as `train_anomaly_artifact`: the prior's GENUINE training rows and features.
    genuine = data.train.features[data.train.labels == settings.prior.classes.index(GENUINE)]
    anomaly = train_anomaly_matrix(genuine, data.feature_list, settings.anomaly)
    # The scenario's payee rule (`fraud._unlisted_mules`): a layer-1 mule the registry never lists.
    seed = config.profiles[profile].seed
    share = config.forwarding.registry_listing_share
    unlisted = [
        account
        for ring in dataset.network.rings
        if ring.persona_key is None
        for account in ring.layers[0]
        if keyed_rng(seed, "mule_listing", str(account)).random() >= share
    ]
    victims = tuple(
        dataset.personas.accounts[key]
        for key in sorted(dataset.personas.accounts)
        if key.startswith("S4_VICTIM")
    )
    assert len(victims) == SESSIONS and unlisted
    return Sensing(
        engine_as(url),
        prior,
        anomaly,
        tuple(data.feature_list),
        victims,
        unlisted[0],
        window.end,
    )


@pytest.fixture(scope="session")
def demo_sensing(
    create_database: Callable[[], str],  # noqa: F811
    kb: KnowledgeBase,
) -> Iterator[Sensing]:
    """Trained once per test session, so any other test that needs `demo` models shares it."""
    sensing = _train_sensing(create_database, kb, "demo")
    yield sensing
    sensing.engine.dispose()


@pytest.fixture(scope="session")
def test_sensing(
    create_database: Callable[[], str],  # noqa: F811
    kb: KnowledgeBase,
) -> Iterator[Sensing]:
    sensing = _train_sensing(create_database, kb, "test")
    yield sensing
    sensing.engine.dispose()


def sensing_prior(sensing: Sensing) -> PriorFn:
    """p_model and q for each S-4 session, as the live Guardian gets them (docs/13 §1).

    Session i is victim i paying the scenario's payee by the scenario's channel, i hours into the
    live window: the payment is inserted, read back with ``load_payment_snapshot`` (the payer's
    real history, the payee's age and mule score) and rolled back. The session's duration is the
    generator's median, standing in for its telemetry. It draws nothing from ``rng``, so the
    harness's own draws are unchanged."""
    settings = load_sensing_config()
    median_s = load_generator_config().telemetry.session_duration_s.median
    _amounts, channel = _scenario()
    sessions = iter(range(SESSIONS))

    def prior_fn(_rng: np.random.Generator, amount_inr: float) -> PriorInputs:
        i = next(sessions)
        payment_id = uuid4()
        with sensing.engine.connect() as conn:
            tx = conn.begin()
            conn.execute(
                insert(bank.payments).values(
                    id=payment_id,
                    payer_account_id=sensing.victims[i],
                    payee_account_id=sensing.payee,
                    amount_paise=round(amount_inr * 100),
                    channel=channel,
                    initiated_at=sensing.start + timedelta(hours=i + 1),
                    status="PENDING_GUARDIAN",
                    origin="DEMO",
                )
            )
            payment, history = load_payment_snapshot(conn, payment_id, settings.features)
            tx.rollback()
        payment = replace(payment, session_duration_s=median_s)
        features = build_payment_features(payment, history, settings.features)
        p_model = sensing.prior.predict({k: features[k] for k in sensing.prior.feature_list})
        q = sensing.anomaly.percentile({k: features[k] for k in sensing.anomaly_features})
        return p_model, q

    return prior_fn


@pytest.mark.slow
def test_s4_prize_advance_fee_sessions_reach_unexplained(
    kb: KnowledgeBase, guardian: CompiledProfile, demo_sensing: Sensing
) -> None:
    """ENG-S4 (docs/21 §3): on the `demo` prior, as docs/14 §5.4 specifies."""
    threshold = kb.config.engine.unexplained_routing_threshold
    results = run_prize_advance_fee_sessions(
        guardian, load_response_models(), sensing_prior(demo_sensing)
    )
    print("S-4 P(UNEXPLAINED) on the demo prior:", [round(p, 3) for p in results])
    share = sum(p >= threshold for p in results) / len(results)
    assert share >= REQUIRED_SHARE, results


def test_s4_pipeline_runs_on_the_test_prior(
    guardian: CompiledProfile, test_sensing: Sensing
) -> None:
    """The PR-CI smoke check (owner decision on #36): the same pipeline on the `test` prior
    runs end to end. Only that it runs is asserted; the 70% check is `demo`'s, nightly."""
    with test_sensing.engine.connect() as conn:
        found = conn.execute(
            select(bank.accounts.c.id).where(bank.accounts.c.id == test_sensing.payee)
        ).scalar_one()
    assert found == test_sensing.payee
    results = run_prize_advance_fee_sessions(
        guardian, load_response_models(), sensing_prior(test_sensing)
    )
    print("S-4 P(UNEXPLAINED) on the test prior:", [round(p, 3) for p in results])
    assert len(results) == SESSIONS
    assert all(0.0 <= p <= 1.0 for p in results)


def _fixture_prior(_rng: np.random.Generator, _amount_inr: float) -> PriorInputs:
    return {
        "GENUINE": 0.97,
        "ATO": 0.01,
        "APP_DIGITAL_ARREST": 0.01,
        "APP_INVESTMENT": 0.005,
        "MULE": 0.005,
    }, 0.95


def test_harness_is_deterministic_with_a_fixture_prior(
    kb: KnowledgeBase, guardian: CompiledProfile
) -> None:
    models = load_response_models()
    first = run_prize_advance_fee_sessions(guardian, models, _fixture_prior)
    assert first == run_prize_advance_fee_sessions(guardian, models, _fixture_prior)
    assert len(first) == SESSIONS
    assert all(0.0 <= p <= 1.0 for p in first)
    # Believing money is coming in is the scenario's signature: no known typology explains it,
    # so some sessions must already lean UNEXPLAINED with a plain prior.
    assert max(first) > kb.config.engine.unexplained_routing_threshold


def test_passive_outcomes_follow_first_match_rules() -> None:
    all_on = SignalDraw(True, True, True, True)
    assert _passive_outcomes(all_on) == {
        "DEVICE_POSTURE": "REMOTE_ACCESS_ACTIVE",
        "SESSION_BEHAVIOUR": "HESITANT_OR_PASTED",
        "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
    }
    cadence_only = SignalDraw(False, False, False, True)
    assert _passive_outcomes(cadence_only)["SESSION_BEHAVIOUR"] == "CADENCE_MISMATCH"
    assert _passive_outcomes(SignalDraw(False, False, False, False)) == {
        "DEVICE_POSTURE": "KNOWN_DEVICE",
        "SESSION_BEHAVIOUR": "NORMAL",
        "CALL_STATUS": "NOT_ON_CALL",
    }
