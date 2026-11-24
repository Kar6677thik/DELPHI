"""The yield-drift detector against the simulator's coaching model (docs/12 §5, docs/14 §5.3,
FR-LRN-05, #121; the owner's option (c) on #121).

A deterministic observation stream: every day, ``PER_DAY`` APP_DIGITAL_ARREST sessions are asked
SPELL_BREAKER_DIGITAL_ARREST. Each answer's yield is the KL divergence (bits) of the KB v1
update from the session's prior, and each day's answers follow the frozen response model's
distribution (``answer_probs``), counted out exactly (no draws). Coaching starts on day ``N``:
the answers move toward the GENUINE row as ``w = shift · clip((day − N) / ramp_days, 0, 1)``.

The prior is the Guardian's for a session whose sensing says DA at 0.2 (the rest GENUINE). The
yield falls as w grows only while the prior leans GENUINE: with a prior that already leans DA, a
coached answer surprises it and the yield rises instead.

What the acceptance criterion ("the alert by day N + 7") needs:
- with docs/14 §5.3's default ramp (shift 0.5 over 14 days), w is 0.25 on day N + 7 and the
  7-day mean is still about 0.9 of the baseline, so the alert comes on day N + 15;
- with a faster, stronger adaptation (shift 1.0 over 7 days), it comes by day N + 7.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np

from delphi.engine import compile_profile
from delphi.engine.belief import kl_bits, update
from delphi.engine.prior import build_prior
from delphi.kb.model import UNEXPLAINED, KnowledgeBase
from delphi.learning.drift import DriftConfig, Observation, daily_yields, drift_alerts
from delphi.platform.settings import Settings
from delphi.sim.response_models import CoachingAdaptation, load_response_models
from delphi.sim.scenarios import ScenarioType

PROBE = "SPELL_BREAKER_DIGITAL_ARREST"
DA = ScenarioType.APP_DIGITAL_ARREST
P_DA = 0.2  # the session's sensing prior: DA at 0.2, the rest GENUINE
PER_DAY = 100  # observations per day, counted out by the answer distribution
DAY0 = date(2026, 1, 1)
CONFIG = DriftConfig.of(Settings(database_url="postgresql://unused/db"))
N = CONFIG.baseline_days + 5  # coaching starts once a full baseline has been seen


def _stream(kb: KnowledgeBase, *, shift: float, ramp_days: float, days: int) -> list[Observation]:
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    source = profile.sources[PROBE]
    p_model = {h: 0.0 for h in profile.hypotheses if h != UNEXPLAINED}
    p_model |= {DA.value: P_DA, "GENUINE": 1.0 - P_DA}
    prior = build_prior(profile, p_model, 0.0)
    yields = [
        kl_bits(update(prior, np.asarray(source.likelihood[o])), prior)
        for o in range(len(source.outcomes))
    ]
    coaching = CoachingAdaptation(
        scenario=DA, probe=PROBE, start_day=N, ramp_days=ramp_days, shift=shift
    )
    models = replace(load_response_models(), adaptations=(coaching,))
    observations: list[Observation] = []
    for day in range(days):
        probs = models.answer_probs(DA, PROBE, sim_day=day)
        counts = np.round(np.asarray(probs) * PER_DAY).astype(int)
        for outcome, count in enumerate(counts):
            observations += [Observation(PROBE, DAY0 + timedelta(days=day), yields[outcome])] * int(
                count
            )
    return observations


def _first_alert_day(observations: list[Observation]) -> int | None:
    alerts = drift_alerts(daily_yields(observations), CONFIG)
    return min(((a.day - DAY0).days for a in alerts), default=None)


def test_a_fast_coaching_ramp_raises_the_alert_within_seven_days(kb: KnowledgeBase) -> None:
    """Assumes shift 1.0 over 7 days (w = 1 by day N + 7)."""
    first = _first_alert_day(_stream(kb, shift=1.0, ramp_days=7, days=N + 8))
    assert first is not None
    assert N < first <= N + 7


def test_the_default_ramp_alerts_only_after_seven_days(kb: KnowledgeBase) -> None:
    """docs/14 §5.3's default (shift 0.5 over 14 days): no alert by day N + 7; the alert comes
    the day after the ramp completes (N + 15). Pinned, so a change to the detector or the KB
    rows that moves it shows up here."""
    observations = _stream(kb, shift=0.5, ramp_days=14, days=N + 30)
    first = _first_alert_day(observations)
    assert first is not None
    assert first > N + 7
    assert first - N == 15  # the day after the ramp completes


def test_no_coaching_no_alert(kb: KnowledgeBase) -> None:
    assert _first_alert_day(_stream(kb, shift=0.0, ramp_days=14, days=N + 30)) is None
