"""S-4 calibration of the collect-refund scenario (docs/14 §5.4, docs/21 §3 ENG-S4).

Under KB v1, at least 70% of UPI_COLLECT_REFUND sessions must end with P(UNEXPLAINED) at or
above the Scout routing threshold (0.35). The sessions' prior comes from the trained sensing
model (docs/13 §4) and the anomaly percentile (§5). Both modules exist (#89, #90); the check runs
once #36 wires them into ``_sensing_prior`` (``SENSING_PRIOR_WIRED``). Until then it is skipped,
and the harness itself is exercised with a fixture prior.

If the check fails, tune only the collect-refund telemetry and answers in ``sim/config`` (never
the KB) and record it in the generator changelog (docs/14 §5.4).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from types import ModuleType

import numpy as np
import pytest

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
from delphi.sim.response_models import (
    ResponseModels,
    SignalDraw,
    draw_signals,
    load_response_models,
)
from delphi.sim.scenarios import ScenarioType

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


# #36 sets this once ``_sensing_prior`` returns the trained models' p_model and q.
SENSING_PRIOR_WIRED = False


def _sensing_prior(sensing: ModuleType) -> PriorFn:
    """The trained sensing model's p_model and q for a collect-refund session (docs/13)."""
    raise NotImplementedError(
        f"wire {sensing.__name__} into the S-4 calibration (docs/14 §5.4) when M3 lands (#36)"
    )


def test_s4_collect_refund_sessions_reach_unexplained(
    kb: KnowledgeBase, guardian: CompiledProfile
) -> None:
    sensing = pytest.importorskip(
        "delphi.sensing.prior", reason="needs the sensing model's prior: M3, #36 (docs/14 §5.4)"
    )
    if not SENSING_PRIOR_WIRED:
        pytest.skip("the trained prior and q are not wired into the S-4 harness yet: #36")
    threshold = kb.config.engine.unexplained_routing_threshold
    results = run_collect_refund_sessions(guardian, load_response_models(), _sensing_prior(sensing))
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
