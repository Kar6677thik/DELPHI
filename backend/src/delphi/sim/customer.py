"""The simulated customer: how a payer answers an active probe (docs/14 §5.2, docs/08 §4, #95).

It reads the payment's ground truth (its scenario, and whether the victim was coached), so it
belongs to ``delphi.sim`` and runs only in the worker, which holds the `delphi_sim` pool
(docs/04 §5.1, docs/17 §2). For each probe it

1. draws the outcome from the frozen response models (``ResponseModels.sample_answer``), on a
   stream keyed by the payment and the probe, so a replay answers the same way; then
2. turns that outcome into the raw answer the customer channel carries (docs/08 §4): an option,
   a latency and app switches for a question, a code for an OTP, a result for a biometric, and
   the counts for a cooling-off. The raw answer is built against the KB's classifier thresholds,
   so the Guardian's own classifier (docs/08 §5) recovers exactly the drawn outcome.

A story runner can register scripted answers for a payment (the persona scripts, docs/14 §6);
probes without a script fall back to the response models.

It never imports the Guardian: the Guardian's ``SimCustomerChannel`` calls ``answer`` and sends
the steps through the same channel contract as the mock phone.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select

from delphi.kb.model import ClassifierThresholds
from delphi.sim.api import current_dataset, history_window
from delphi.sim.response_models import ResponseModels
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType
from delphi.sim.truth import payment_truth, scenario_instances

# The purposes a customer gives when nothing is wrong, and the high-risk ones each scam pushes
# its victim towards (docs/08 §5 lists the high-risk set).
NORMAL_PURPOSES = (
    "FAMILY_OR_FRIEND",
    "BILLS_OR_SHOPPING",
    "MEDICAL",
    "EDUCATION",
    "PROPERTY_OR_LOAN",
)
HIGH_RISK_BY_SCENARIO: Mapping[ScenarioType, tuple[str, ...]] = {
    ScenarioType.APP_DIGITAL_ARREST: ("GOVT_POLICE_OR_COURT_FEE", "SAFE_ACCOUNT_TRANSFER"),
    ScenarioType.APP_INVESTMENT: ("INVESTMENT",),
}
HIGH_RISK_ANY = (
    "INVESTMENT",
    "GOVT_POLICE_OR_COURT_FEE",
    "SAFE_ACCOUNT_TRANSFER",
    "CUSTOMS_OR_PARCEL",
)
RECEIVING = "RECEIVING_REFUND_OR_PRIZE"

CONFIG_FILE = Path(__file__).resolve().parent / "config" / "customer.yaml"


class CustomerTimings(BaseModel):
    """``sim/config/customer.yaml``: the answer timings, in seconds (gap fills)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quick_answer_s: tuple[float, float]
    otp_typing_s: tuple[float, float]
    margin_s: float = Field(gt=0)
    slow_extra_s: tuple[float, float]
    cooling_off_slack_s: float = Field(gt=0)
    min_latency_s: float = Field(gt=0)


def load_timings(path: Path = CONFIG_FILE) -> CustomerTimings:
    return CustomerTimings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class AnswerStep(NamedTuple):
    """One answer the customer sends: the docs/08 §4 ``answer`` body, how long after the probe
    was shown, and how often the app was switched away meanwhile."""

    answer: Mapping[str, Any]
    latency_s: float
    app_switches: int = 0


@dataclass(frozen=True, slots=True)
class _Truth:
    scenario: ScenarioType
    coached: bool


@dataclass
class SimulatedCustomer:
    """Answers active probes for simulated payments (worker only: it reads ground truth)."""

    sim_engine: Engine
    models: ResponseModels
    thresholds: ClassifierThresholds
    seed: int
    history_start: datetime
    timings: CustomerTimings = field(default_factory=load_timings)
    scripts: dict[UUID, dict[str, Sequence[AnswerStep]]] = field(default_factory=dict)

    def script(self, payment_id: UUID, answers: Mapping[str, Sequence[AnswerStep]]) -> None:
        """Scripted answers for one payment (a persona story, docs/14 §6)."""
        self.scripts[payment_id] = {k: tuple(v) for k, v in answers.items()}

    def answer(
        self,
        *,
        payment_id: UUID,
        probe_key: str,
        options: Sequence[str],
        otp_code: str | None,
        timeout_s: float,
        at: datetime,
    ) -> list[AnswerStep]:
        """The steps the customer sends for this probe, in order (several only for an OTP
        typed wrong until the attempts run out)."""
        scripted = self.scripts.get(payment_id, {}).get(probe_key)
        if scripted is not None:
            return list(scripted)
        truth = self._truth(payment_id)
        rng = keyed_rng(self.seed, "answer", str(payment_id), probe_key)
        sim_day = (at - self.history_start) / timedelta(days=1)
        outcome = self.models.sample_answer(
            rng, truth.scenario, probe_key, coached=truth.coached, sim_day=sim_day
        )
        return raw_answer(
            probe_key,
            outcome,
            rng,
            self.thresholds,
            scenario=truth.scenario,
            options=options,
            otp_code=otp_code,
            timeout_s=timeout_s,
            timings=self.timings,
        )

    def _truth(self, payment_id: UUID) -> _Truth:
        pt, si = payment_truth.c, scenario_instances.c
        with self.sim_engine.connect() as conn:
            row = conn.execute(
                select(si.scenario_type, pt.coached)
                .select_from(
                    payment_truth.join(scenario_instances, si.id == pt.scenario_instance_id)
                )
                .where(pt.payment_id == payment_id)
            ).one_or_none()
        if row is None:
            raise LookupError(f"payment {payment_id} has no simulated truth")
        return _Truth(ScenarioType(row.scenario_type), bool(row.coached))


@dataclass
class LiveCustomer:
    """The simulated customer of the live bank, for the worker's Guardian runner (#95, #101).

    The worker starts before the bank holds a dataset, so the seed and the history's start that
    ``SimulatedCustomer`` keys its draws by are read from the dataset in the bank when a probe
    is answered (one bounded row), and the customer is rebuilt when that dataset changes. The
    story scripts (``script``) are kept in memory across datasets: a worker restart forgets
    them, and the story session's remaining probes are answered from the response models with
    the story's ground truth (docs/14 §10)."""

    sim_engine: Engine
    models: ResponseModels
    thresholds: ClassifierThresholds
    scripts: dict[UUID, dict[str, Sequence[AnswerStep]]] = field(default_factory=dict)
    _current: tuple[UUID, SimulatedCustomer] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def script(self, payment_id: UUID, answers: Mapping[str, Sequence[AnswerStep]]) -> None:
        """Scripted answers for one payment (a persona story, docs/14 §6)."""
        with self._lock:
            self.scripts[payment_id] = {k: tuple(v) for k, v in answers.items()}

    def answer(
        self,
        *,
        payment_id: UUID,
        probe_key: str,
        options: Sequence[str],
        otp_code: str | None,
        timeout_s: float,
        at: datetime,
    ) -> list[AnswerStep]:
        return self._customer().answer(
            payment_id=payment_id,
            probe_key=probe_key,
            options=options,
            otp_code=otp_code,
            timeout_s=timeout_s,
            at=at,
        )

    def _customer(self) -> SimulatedCustomer:
        with self.sim_engine.connect() as conn:
            dataset = current_dataset(conn)
        if dataset is None:
            raise LookupError("the bank holds no dataset")
        with self._lock:
            if self._current is None or self._current[0] != dataset.id:
                customer = SimulatedCustomer(
                    sim_engine=self.sim_engine,
                    models=self.models,
                    thresholds=self.thresholds,
                    seed=dataset.seed,
                    history_start=history_window(dataset).start,
                    scripts=self.scripts,  # shared: a script registered later still counts
                )
                self._current = (dataset.id, customer)
            return self._current[1]


def raw_answer(
    probe_key: str,
    outcome: str,
    rng: np.random.Generator,
    t: ClassifierThresholds,
    *,
    scenario: ScenarioType,
    options: Sequence[str],
    otp_code: str | None,
    timeout_s: float,
    timings: CustomerTimings | None = None,
) -> list[AnswerStep]:
    """A raw answer that the docs/08 §5 classifier turns back into ``outcome``."""
    c = timings or load_timings()
    quick, typing, slow_extra, margin = c.quick_answer_s, c.otp_typing_s, c.slow_extra_s, c.margin_s

    def between(low: float, high: float) -> float:
        return float(rng.uniform(low, high))

    def pick(keys: Sequence[str]) -> str:
        offered = [k for k in keys if k in options]
        if not offered:  # another question bank: never answer with a key of another outcome
            raise ValueError(f"{probe_key}: none of {list(keys)} is among the options shown")
        return offered[int(rng.integers(len(offered)))]

    def hesitant(threshold: float, *, fast_floor: float | None = None) -> tuple[float, int]:
        """Slower than ``threshold`` or with an app switch (or faster than ``fast_floor``)."""
        kinds = ["slow", "switch"] + (["fast"] if fast_floor is not None else [])
        kind = kinds[int(rng.integers(len(kinds)))]
        if kind == "slow":
            return threshold + between(*slow_extra), 0
        if kind == "fast" and fast_floor is not None:
            return between(margin / 2, fast_floor - margin), 0
        return between(*quick), 1

    if probe_key == "PURPOSE_QUESTION":
        normal = (t.purpose_fast_s + margin, t.purpose_slow_s - margin)
        if outcome == "BELIEVES_RECEIVING_MONEY":
            return [AnswerStep({"option_key": pick((RECEIVING,))}, between(*normal))]
        if outcome == "HIGH_RISK_PURPOSE":
            keys = HIGH_RISK_BY_SCENARIO.get(scenario, HIGH_RISK_ANY)
            return [AnswerStep({"option_key": pick(keys)}, between(*normal))]
        option = {"option_key": pick(NORMAL_PURPOSES)}
        if outcome == "TIMING_ANOMALOUS":
            latency, switches = hesitant(t.purpose_slow_s, fast_floor=t.purpose_fast_s)
            return [AnswerStep(option, latency, switches)]
        return [AnswerStep(option, between(*normal))]  # PLAUSIBLE_NORMAL_TIMING
    if probe_key.startswith("SPELL_BREAKER_"):
        if outcome == "ANSWERED_YES":
            return [AnswerStep({"option_key": "YES"}, between(*quick))]
        if outcome == "HESITATION_OR_APP_SWITCH":
            latency, switches = hesitant(t.spell_breaker_hesitation_s)
            return [AnswerStep({"option_key": "NO"}, latency, switches)]
        normal_no = between(quick[0], t.spell_breaker_hesitation_s - margin)
        return [AnswerStep({"option_key": "NO"}, normal_no)]  # ANSWERED_NO_NORMAL
    if probe_key == "COLLECT_INTENT_CHECK":
        key = "YES_RECEIVING" if outcome == "EXPECTS_TO_RECEIVE" else "NO_I_AM_PAYING"
        return [AnswerStep({"option_key": pick((key,))}, between(*quick))]
    if probe_key == "BIOMETRIC_STEP_UP":
        result = "MATCH" if outcome == "PASSED" else "CANCEL"
        return [AnswerStep({"biometric": result}, between(*quick))]
    if probe_key == "OTP_STEP_UP":
        if outcome == "PASSED" and otp_code is not None:
            return [AnswerStep({"otp": otp_code}, between(*typing))]
        wrong = _wrong_code(otp_code, rng)
        per_attempt = min(typing[1], timeout_s / (t.otp_max_attempts + 1))
        low = min(typing[0], per_attempt / 2)
        return [
            AnswerStep({"otp": wrong}, between(low, per_attempt)) for _ in range(t.otp_max_attempts)
        ]
    if probe_key == "COOLING_OFF":
        counts: dict[str, Any] = {"cancelled": outcome == "CANCELLED_BY_CUSTOMER"}
        if outcome == "PERSISTED_OR_ESCALATED":
            counts |= {"send_now_presses": t.cooling_off_send_now_presses}
        # the customer waits the window out (or cancels within it), then the answer arrives
        latency = max(timeout_s - c.cooling_off_slack_s, c.min_latency_s)
        return [AnswerStep({"cooling_off": counts}, latency)]
    raise ValueError(f"{probe_key} has no simulated answer")


def _wrong_code(code: str | None, rng: np.random.Generator) -> str:
    while True:
        candidate = f"{int(rng.integers(10**6)):06d}"
        if candidate != code:
            return candidate
