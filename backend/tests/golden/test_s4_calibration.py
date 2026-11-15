"""S-4 calibration of the collect-refund scenario (docs/14 §5.4, docs/21 §3 ENG-S4).

Under KB v1, at least 70% of UPI_COLLECT_REFUND sessions must end with P(UNEXPLAINED) at or
above the Scout routing threshold (0.35). Each session's prior comes from sensing models
trained here on the `test` profile (#36, Option A): the prior model (docs/13 §4) trained by the
same code as `TRAIN_SENSING` on the labels the seed delivers, and the anomaly percentile (§5)
from an isolation forest on the GENUINE training payments. A small model and a weak prior: the
honest case for S-4. The check needs PostgreSQL (``DELPHI_TEST_DATABASE_URL``); the harness
itself is also exercised with a fixture prior.

If the check fails, tune only the collect-refund telemetry and answers in ``sim/config`` (never
the KB) and record it in the generator changelog (docs/14 §5.4).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import select

from delphi.engine import (
    CompiledProfile,
    Phase,
    SessionFacts,
    build_prior,
    initial_budgets,
    plan_next,
    update,
)
from delphi.kb.model import UNEXPLAINED, KnowledgeBase
from delphi.sensing.anomaly import AnomalyModel, train_anomaly
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import (
    CustomerHistory,
    PaymentView,
    build_payment_features,
    payment_feature_names,
    to_us,
)
from delphi.sensing.offline import OfflineHistories, load_bank_frames
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
from delphi.sim.scenarios import ScenarioType
from delphi.sim.writer import write_dataset

from ..integration._db import engine_as, upgrade

REFUND = ScenarioType.UPI_COLLECT_REFUND
SESSIONS = 15  # docs/14 §6: 15 collect-refund victims on `demo`
REQUIRED_SHARE = 0.70
AMOUNT_INR = (2_000, 50_000)  # docs/14 §4, log-uniform
CHANNEL = "UPI_COLLECT_APPROVAL"
SEED = 20261031  # the `demo` profile seed (docs/14 §2)

# (p_model, anomaly percentile q) for one session.
PriorInputs = tuple[Mapping[str, float], float]
PriorFn = Callable[[np.random.Generator, float], PriorInputs]


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


def run_collect_refund_sessions(
    profile: CompiledProfile, models: ResponseModels, prior_fn: PriorFn
) -> list[float]:
    """Terminal P(UNEXPLAINED) of each S-4 session: passive phase, then planned probes answered
    from the frozen collect-refund response model."""
    rng = np.random.Generator(np.random.PCG64(SEED))
    unexplained = profile.hypotheses.index(UNEXPLAINED)
    facts = SessionFacts(channel=CHANNEL, consents=frozenset({"call_status"}))
    results = []
    for _ in range(SESSIONS):
        amount_inr = math.exp(rng.uniform(math.log(AMOUNT_INR[0]), math.log(AMOUNT_INR[1])))
        p_model, q = prior_fn(rng, amount_inr)
        belief = build_prior(profile, p_model, q)
        observed: set[str] = set()
        passive = _passive_outcomes(draw_signals(rng, models.telemetry_targets[REFUND]))
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
            answer = models.sample_answer(rng, REFUND, plan.action)
            belief = update(belief, src.likelihood[src.outcomes.index(answer)])
            observed.add(plan.action)
            budgets = budgets.after(src)
        results.append(float(belief[unexplained]))
    return results


@dataclass(frozen=True)
class Victim:
    """An S-4 victim as the bank sees them: an ordinary customer with their own genuine history
    and no scam in it (docs/14 §6)."""

    account_id: str
    history: CustomerHistory


@dataclass(frozen=True)
class Sensing:
    """The small sensing models trained on `test`, and what a collect-refund payment needs."""

    prior: PriorModel
    anomaly: AnomalyModel
    victims: tuple[Victim, ...]
    payee_account_id: str  # a first-layer mule of the persona ring (the refund's payee)
    payee_opened_at: datetime
    start: datetime  # the live window opens when the history ends


@pytest.fixture(scope="session")
def sensing(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Sensing]:
    url = create_database()
    upgrade(url)
    settings = load_sensing_config()
    dataset = generate_dataset(load_generator_config(), "test", kb=kb, personas=True)
    assert dataset.personas is not None
    window = bank.history_window(DatasetInfo(uuid4(), "test", 1, datetime.now(UTC)))
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:  # the bank as the seed leaves it: the history's labels delivered
        write_dataset(conn, dataset)
        deliver_labels(
            conn, until=window.end, active_hypotheses=frozenset(kb.hypotheses), now=window.end
        )
    sim.dispose()
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            data = load_prior_data(conn, window, settings)
            frames = load_bank_frames(conn)
            ring = next(iter(sorted(dataset.personas.rings.items())))[1]
            payee = ring[0][0]
            opened = conn.execute(
                select(bank.accounts.c.opened_at).where(bank.accounts.c.id == payee)
            ).scalar_one()
    finally:
        app.dispose()
    prior, _ = train_prior(
        data.train, data.validation, data.test, data.feature_list, settings.prior, profile="test"
    )
    # docs/13 §5: the forest learns the GENUINE training payments, over every payment feature.
    names = payment_feature_names(settings.features)
    genuine = [
        i
        for i, code in zip(data.ids[0], data.train.labels, strict=True)
        if settings.prior.classes[code] == "GENUINE"
    ]
    rows = OfflineHistories(frames, settings.features).features(genuine)[list(names)]
    anomaly = train_anomaly(rows.to_dict("records"), names, settings.anomaly)
    histories = OfflineHistories(frames, settings.features)
    victims = tuple(
        Victim(str(dataset.personas.accounts[key]), histories.history(str(cid)))
        for key, cid in sorted(dataset.personas.customers.items())
        if key.startswith("S4_VICTIM")
    )
    assert len(victims) == SESSIONS
    yield Sensing(prior, anomaly, victims, str(payee), opened, window.end)


def _sensing_prior(sensing: Sensing) -> PriorFn:
    """p_model and q for each collect-refund session, from the trained models (docs/13 §1).

    Session i is victim i, with their own history, paying the ring's first-layer mule (a payee
    they never saved) through
    UPI collect approval, i hours into the live window, with the generator's median session
    length. It draws nothing from ``rng``, so the harness's own draws are unchanged.
    """
    settings = load_sensing_config()
    median_s = load_generator_config().telemetry.session_duration_s.median
    sessions = iter(range(SESSIONS))

    def prior_fn(_rng: np.random.Generator, amount_inr: float) -> PriorInputs:
        i = next(sessions)
        victim = sensing.victims[i]
        payment = PaymentView(
            id=f"s4-{i:02d}",
            payer_account_id=victim.account_id,
            payee_account_id=sensing.payee_account_id,
            amount_paise=round(amount_inr * 100),
            channel=CHANNEL,
            initiated_at_us=to_us(sensing.start + timedelta(hours=i + 1)),
            device_id=None,
            payee_account_opened_at_us=to_us(sensing.payee_opened_at),
            payee_p_mule=settings.features.mule_score_fallback,
            session_duration_s=median_s,
            remote_access_active=None,
        )
        features = build_payment_features(payment, victim.history, settings.features)
        p_model = sensing.prior.predict({k: features[k] for k in sensing.prior.feature_list})
        return p_model, sensing.anomaly.percentile(features)

    return prior_fn


def test_s4_collect_refund_sessions_reach_unexplained(
    kb: KnowledgeBase, guardian: CompiledProfile, sensing: Sensing
) -> None:
    threshold = kb.config.engine.unexplained_routing_threshold
    results = run_collect_refund_sessions(guardian, load_response_models(), _sensing_prior(sensing))
    print("S-4 P(UNEXPLAINED):", [round(p, 3) for p in results])
    share = sum(p >= threshold for p in results) / len(results)
    assert share >= REQUIRED_SHARE, results


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
    """The harness runs today; only the prior waits for sensing."""
    models = load_response_models()
    first = run_collect_refund_sessions(guardian, models, _fixture_prior)
    assert first == run_collect_refund_sessions(guardian, models, _fixture_prior)
    assert len(first) == SESSIONS
    assert all(0.0 <= p <= 1.0 for p in first)
    # Believing money is coming in is the collect-refund signature: no known typology explains
    # it, so some sessions must already lean UNEXPLAINED with a plain prior.
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
