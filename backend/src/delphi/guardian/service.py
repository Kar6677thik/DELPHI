"""The Guardian session service (docs/08 §2–3, #94): one application service for every entry
point (the mock phone, the simulation, the evaluation harness, docs/08 §1).

Each step is a method, and each commits before it returns (docs/08 §2: "after every step the
session row and its probe runs are committed"), so a session can be resumed between steps:

- ``start``      steps 1–3: create the session, pin the KB, build the prior, fast path
- ``passive``    step 4:   every eligible passive probe in parallel, updates in KB order
- ``plan``       steps 5–6, 8: plan the next active probe (a PENDING probe run) or decide
- ``fail_safe``  any step's unrecoverable error → HOLD_FOR_REVIEW / ENGINE_ERROR (FR-GDN-13)

Issuing an active probe and recording its response, timeouts and cancel come with #94 part 3;
running the steps as one asyncio task per session, and resuming them, with #95.

The prior's inputs come through ``PriorPort`` (the sensing inference, #92). MULE comes from the
payer's mule score (``with_payer_mule``, docs/06 §11). The LLM is never consulted (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Executor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import numpy as np
from sqlalchemy import Connection, distinct, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.engine.belief import kl_bits
from delphi.engine.compile import compile_profile
from delphi.engine.exploration import session_seed
from delphi.engine.planner import (
    SessionFacts,
    static_ineligibility,
)
from delphi.engine.risk import loss_matrix, risk_by_decision, voi_myopic
from delphi.engine.types import CompiledProfile, Phase, PlanResult
from delphi.guardian import ddpack
from delphi.guardian.active import (
    STEP_UPS,
    ClientSignals,
    Ui,
    biometric_observation,
    cooling_off_observation,
    new_otp,
    otp_code,
    otp_digest,
    otp_matches,
    question_observation,
    response_latency_s,
    timed_out_observation,
    ui_for,
)
from delphi.guardian.procedure import (
    final_decision,
    observe,
    plan_step,
    session_prior,
    stop_reason,
    takes_fast_path,
)
from delphi.guardian.store import (
    GuardianSessionRecord,
    ProbeMode,
    ProbeRunStatus,
    Runner,
    SelectedBy,
    SessionMode,
    SessionStatus,
    TerminalReason,
    add_probe_run,
    create_session,
    get_session,
    list_probe_runs,
)
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.kb.model import UNEXPLAINED
from delphi.kb.store import StoredVersion, active_version, get_version
from delphi.navigator import api as navigator
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.logging import get_logger
from delphi.probes.api import (
    GUARDIAN_CLASSIFIERS,
    Fetcher,
    OtpObs,
    PassiveRequest,
    PassiveStatus,
    ServedQuestion,
    approved_variants,
    run_passive_probes,
    serve_question,
)
from delphi.sensing.api import PriorInputs
from delphi.sim import api as bank

PROFILE = "GUARDIAN_PAYMENT"
FALLBACK_LANGUAGE = "en"
CALL_STATUS_CONSENT = "call_status"  # kb/sources/probes/CALL_STATUS.yaml requires_consent
FAIRNESS_WINDOW = timedelta(days=30)  # docs/06 §8: the rolling 30 days of the fairness cap
DONE = frozenset({SessionStatus.DECIDED, SessionStatus.CANCELLED_BY_CUSTOMER})
GENUINE = "GENUINE"

log = get_logger(__name__)


class PriorPort(Protocol):
    """The sensing inference (``delphi.sensing.api.SensingInference``, #92). Reads only data
    from before the payment's initiation."""

    def prior_inputs(self, payment: bank.PaymentView) -> PriorInputs: ...


@dataclass(frozen=True, slots=True)
class Decided:
    session: GuardianSessionRecord


@dataclass(frozen=True, slots=True)
class NextProbe:
    """The planner chose an active probe: a PENDING probe run to issue (#94 part 3)."""

    session: GuardianSessionRecord
    probe_run_id: UUID
    probe_key: str
    plan: PlanResult | None  # None when an earlier call already chose it (a resume)


PlanOutcome = Decided | NextProbe

# Errors that are the caller's (a bad answer, a wrong state): returned as problems, never
# turned into an ENGINE_ERROR hold.
CALLER_ERRORS = frozenset(
    {
        ProblemCode.INVALID_STATE_TRANSITION,
        ProblemCode.VALIDATION_FAILED,
        ProblemCode.INVALID_CLIENT_SIGNALS,
    }
)
COOLING_OFF = "COOLING_OFF"
CANCELLED_BY_CUSTOMER = "CANCELLED_BY_CUSTOMER"  # COOLING_OFF's outcome that ends the session


@dataclass(frozen=True, slots=True)
class IssuedProbe:
    """What the customer channel shows (docs/08 §4). ``otp_code`` is for the channel's mock SMS
    only: the probe run stores a digest, never the code."""

    session: GuardianSessionRecord
    probe_run_id: UUID
    probe_key: str
    ui: Ui
    timeout_s: float
    question: ServedQuestion | None = None
    otp_code: str | None = None


@dataclass(frozen=True, slots=True)
class Responded:
    """``completed`` is false only for a wrong OTP with attempts left."""

    session: GuardianSessionRecord
    completed: bool
    outcome: str | None = None
    attempts_remaining: int | None = None


@dataclass(slots=True)
class _Context:
    """Everything a step needs, loaded fresh in its transaction."""

    record: GuardianSessionRecord
    version: StoredVersion
    profile: CompiledProfile
    payment: bank.PaymentView
    language: str
    vulnerable: bool
    consents: frozenset[str]
    prior_components: dict[str, Any]  # what the prior was built from, for the case (docs/09 §1)
    stored_risks: dict[str, float] | None
    belief: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def losses(self) -> np.ndarray:
        return loss_matrix(self.profile, float(self.payment.amount_paise), self.vulnerable)

    def as_dict(self, belief: np.ndarray) -> dict[str, float]:
        return {h: float(p) for h, p in zip(self.profile.hypotheses, belief, strict=True)}


class GuardianService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        prior: PriorPort,
        fetchers: Mapping[str, Fetcher],
        executor: Executor,
        *,
        otp_key: bytes,
        demo_time: bool = False,
        explore: bool = True,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._prior = prior
        self._fetchers = fetchers
        self._executor = executor
        self._otp_key = otp_key  # Settings.otp_key: keys the OTP digest (#171)
        self._demo_time = demo_time
        self._explore = explore  # False: greedy only (evaluation ablations, replays)
        self._profiles: dict[int, CompiledProfile] = {}

    # ------------------------------------------------------------------ steps

    def start(
        self, payment_id: UUID, *, mode: SessionMode, runner: Runner
    ) -> GuardianSessionRecord:
        """Steps 1–3. Creates the session (committed on its own, so a later failure still has a
        session to hold), then builds the prior and takes the fast path if it applies."""
        with self._sessions.begin() as s:
            payment = bank.get_payment(s, payment_id)
            if payment is None:
                raise NotFoundError(f"No payment {payment_id}.")
            if payment.status != bank.PaymentStatus.PENDING_GUARDIAN:
                raise DelphiError(
                    f"Payment {payment_id} is not waiting for the Guardian.",
                    code=ProblemCode.INVALID_STATE_TRANSITION,
                )
            pinned = active_version(s)
            if pinned is None:
                raise DelphiError("No ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
            seed = session_seed(str(payment_id), pinned.kb.config.engine.exploration_seed)
            created = create_session(
                s, self._clock, payment_id=payment_id, mode=mode, runner=runner, rng_seed=seed
            )
            record(
                s,
                self._clock,
                actor_type=ActorType.ENGINE,
                actor_id=None,
                action="guardian.session.created",
                entity_type="guardian_session",
                entity_id=str(created.id),
                payload={"payment_id": str(payment_id), "kb_version_id": created.kb_version_id},
            )
            publish(s, EventType.GUARDIAN_SESSION_UPDATED, str(created.id), _event(created))
        return self._guarded(created.id, self._build_prior)

    def _build_prior(self, s: Session, ctx: _Context) -> GuardianSessionRecord:
        inputs = self._prior.prior_inputs(ctx.payment)
        profile = ctx.profile
        built = session_prior(profile, inputs)
        prior = built.prior
        risks = risk_by_decision(prior, ctx.losses())
        prior_probs = ctx.as_dict(prior)
        # B1/B2's verdicts for the monitor (docs/08 §6, FR-GDN-12): B2 from this same prior.
        baselines = bank.baseline_decisions(
            s,
            ctx.payment,
            p_genuine=prior_probs.get(GENUINE, 0.0),
            new_device_days=ctx.version.kb.config.classifiers.new_device_days,
        )
        self._write(
            s,
            ctx.record,
            baseline_decisions=baselines,
            prior=prior_probs,
            p_model=built.p_model,
            anomaly_percentile=built.anomaly_percentile,
            open_mass=built.open_mass,
            open_mass_shares=built.open_mass_shares,
            top_features=[dict(f) for f in inputs.top_features],
            posterior=ctx.as_dict(prior),
            risk_by_decision=_risks(profile, risks),
        )
        if takes_fast_path(ctx.version.kb, built, ctx.payment.amount_paise):
            return self._decide(s, self._load(s, ctx.record.id), prior, TerminalReason.FAST_PATH)
        return _reload(s, ctx.record.id)

    async def passive(self, session_id: UUID) -> GuardianSessionRecord:
        """Step 4: every eligible passive probe in parallel, each with the KB's adapter timeout.
        A timeout or adapter error is an ERROR run with no update (FR-GDN-03); updates apply in
        KB source order. A CREATED session is probed; so is one left in PASSIVE_PROBING by a
        crash (its probe runs are written in the same transaction as the move to PLANNING, so
        it has none). The results apply only if the session is still the one this call moved:
        a cancel in the meantime wins (docs/08 §2)."""
        with self._sessions.begin() as s:
            ctx = self._load(s, session_id)
            if ctx.record.status is SessionStatus.CREATED:
                probing = self._write(s, ctx.record, status=SessionStatus.PASSIVE_PROBING.value)
            elif ctx.record.status is SessionStatus.PASSIVE_PROBING:
                probing = self._write(s, ctx.record)  # a resume: claim it with a new version
            else:
                return ctx.record
            facts = self._facts(s, ctx)
            keys = [
                k
                for k, src in ctx.profile.sources.items()
                if static_ineligibility(
                    ctx.profile, src, facts, frozenset(), Phase.GUARDIAN_PASSIVE
                )
                is None
            ]
            request = PassiveRequest(
                ctx.payment.id,
                _require(ctx.payment.payer_customer_id),
                ctx.payment.payee_account_id,
                ctx.payment.device_id,
                ctx.payment.initiated_at,
            )
            kb = ctx.version.kb
        try:
            results = await run_passive_probes(
                keys,
                self._fetchers,
                request,
                kb.config.classifiers,
                kb.config.guardrails.passive_adapter_timeout_ms,
                executor=self._executor,
            )
        except Exception as exc:  # the runner never raises; anything here is a bug
            return self.fail_safe(session_id, exc)

        def apply(s: Session, ctx: _Context) -> GuardianSessionRecord:
            if (
                ctx.record.status is not SessionStatus.PASSIVE_PROBING
                or ctx.record.version != probing.version
            ):
                return ctx.record  # cancelled, or claimed by a newer resume: not ours to apply
            losses = ctx.losses()
            prior = ctx.belief
            belief = prior
            for result in results:  # in KB source order (keys were taken in that order)
                src = ctx.profile.sources[result.source_key]
                run = add_probe_run(
                    s, self._clock, session_id, probe_key=src.key, mode=ProbeMode.PASSIVE
                )
                voi = voi_myopic(prior, src.likelihood, losses)  # analytics only (docs/06 §9)
                values: dict[str, Any] = {
                    "voi_myopic_paise": round(voi),
                    "net_value_paise": round(voi - src.cost_paise),
                    "cost_paise": round(src.cost_paise),
                    "posterior_before": ctx.as_dict(belief),
                    "completed_at": self._clock.now(),
                }
                if result.status is PassiveStatus.OK and result.outcome is not None:
                    after = observe(ctx.profile, belief, src.key, result.outcome)
                    values |= {
                        "status": ProbeRunStatus.COMPLETED.value,
                        "outcome": result.outcome,
                        "posterior_after": ctx.as_dict(after),
                        "info_yield_bits": kl_bits(after, belief),
                    }
                    belief = after
                else:
                    values |= {
                        "status": ProbeRunStatus.ERROR.value,
                        "raw_response": {"error": result.error},
                        "posterior_after": ctx.as_dict(belief),
                    }
                s.execute(update(probe_runs).where(probe_runs.c.id == run.id).values(**values))
            updated = self._write(
                s,
                ctx.record,
                status=SessionStatus.PLANNING.value,
                posterior=ctx.as_dict(belief),
                risk_by_decision=_risks(ctx.profile, risk_by_decision(belief, losses)),
            )
            publish(s, EventType.GUARDIAN_SESSION_UPDATED, str(session_id), _event(updated))
            return updated

        return self._guarded(session_id, apply)

    def plan(self, session_id: UUID) -> PlanOutcome:
        """Steps 5, 6 and 8: plan the next active probe, or decide. A chosen probe becomes a
        PENDING active probe run carrying the candidates table, ready to issue."""
        outcome: list[PlanOutcome] = []

        def step(s: Session, ctx: _Context) -> GuardianSessionRecord:
            # One planner at a time per session: two calls (or a resume racing a live step)
            # must not both add an active run (docs/05 §2).
            s.execute(
                select(guardian_sessions.c.id)
                .where(guardian_sessions.c.id == session_id)
                .with_for_update()
            )
            ctx = self._load(s, session_id)
            if ctx.record.status is not SessionStatus.PLANNING:
                raise DelphiError(
                    f"Session {session_id} is {ctx.record.status}, not PLANNING.",
                    code=ProblemCode.INVALID_STATE_TRANSITION,
                )
            pending = s.execute(
                select(probe_runs.c.id, probe_runs.c.probe_key).where(
                    probe_runs.c.session_id == session_id,
                    probe_runs.c.status == ProbeRunStatus.PENDING.value,
                )
            ).one_or_none()
            if pending is not None:  # already chosen, not yet issued (a resume after a crash)
                outcome.append(NextProbe(ctx.record, pending.id, pending.probe_key, None))
                return ctx.record
            guardrails = ctx.version.kb.config.guardrails
            if _session_age(s, ctx.record, self._clock.now()) > timedelta(
                seconds=guardrails.session_max_duration_s
            ):
                decided = self._decide(s, ctx, ctx.belief, TerminalReason.SESSION_TIMEOUT)
                outcome.append(Decided(decided))
                return decided
            runs = list_probe_runs(s, session_id)
            ran = tuple(r.probe_key for r in runs)
            active = sum(1 for r in runs if r.mode is ProbeMode.ACTIVE)
            plan = plan_step(
                ctx.profile,
                ctx.belief,
                amount_paise=ctx.payment.amount_paise,
                vulnerable=ctx.vulnerable,
                facts=self._facts(s, ctx),
                ran=ran,
                active=active,
                rng_seed=ctx.record.rng_seed,
                explore=self._explore,
            )
            if plan.is_stop:
                decided = self._decide(s, ctx, ctx.belief, stop_reason(plan), plan=plan)
                outcome.append(Decided(decided))
                return decided
            src = ctx.profile.sources[plan.action]
            run = add_probe_run(
                s,
                self._clock,
                session_id,
                probe_key=plan.action,
                mode=ProbeMode.ACTIVE,
                selected_by=SelectedBy(plan.selected_by.value) if plan.selected_by else None,
                propensity=plan.propensity,
            )
            chosen = next(c for c in plan.candidates if c.source_key == plan.action)
            s.execute(
                update(probe_runs)
                .where(probe_runs.c.id == run.id)
                .values(
                    eligible_set=[c.source_key for c in plan.candidates if c.eligible],
                    candidates=_candidates(plan),
                    lookahead_depth=plan.lookahead_depth,
                    voi_myopic_paise=_paise(chosen.voi_myopic_paise),
                    net_value_paise=_paise(chosen.net_value_paise),
                    cost_paise=round(src.cost_paise),
                    posterior_before=ctx.as_dict(ctx.belief),
                )
            )
            current = _reload(s, session_id)
            outcome.append(NextProbe(current, run.id, plan.action, plan))
            return current

        decided = self._guarded(session_id, step)
        return outcome[0] if outcome else Decided(decided)

    def fail_safe(self, session_id: UUID, error: BaseException) -> GuardianSessionRecord:
        """FR-GDN-13: an unrecoverable error decides HOLD_FOR_REVIEW with terminal_reason
        ENGINE_ERROR, in a fresh transaction, without running the engine. A finished session is
        left as it is."""
        log.error("guardian.engine_error", session_id=str(session_id), exc_info=error)
        try:
            with self._sessions.begin() as s:
                current = _reload(s, session_id)
                if current.status in DONE:
                    return current
                ctx = self._load(s, session_id)
                return self._decide(
                    s, ctx, ctx.belief, TerminalReason.ENGINE_ERROR, decision="HOLD_FOR_REVIEW"
                )
        except Exception as again:
            # The full fail-safe failed too (e.g. the bank or the case step). End the session
            # anyway, so nothing resumes it into a loop; the payment and the case are left to
            # an operator (the error is logged and audited).
            log.critical("guardian.fail_safe_failed", session_id=str(session_id), exc_info=again)
            with self._sessions.begin() as s:
                current = _reload(s, session_id)
                if current.status in DONE:
                    return current
                now = self._clock.now()
                _close_open_runs(s, session_id, "fail_safe", now)
                decided = self._write(
                    s,
                    current,
                    status=SessionStatus.DECIDED.value,
                    decision="HOLD_FOR_REVIEW",
                    terminal_reason=TerminalReason.ENGINE_ERROR.value,
                    decided_at=now,
                )
                record(
                    s,
                    self._clock,
                    actor_type=ActorType.ENGINE,
                    actor_id=None,
                    action="guardian.session.fail_safe_failed",
                    entity_type="guardian_session",
                    entity_id=str(session_id),
                    payload={"error": type(again).__name__},
                )
                return decided

    # ------------------------------------------------------------------ active probes (step 7)

    def issue(self, probe_run_id: UUID) -> IssuedProbe | Decided:
        """Send a PENDING active probe to the customer: the run and the session become
        AWAITING_CUSTOMER. A question gets its variant (docs/07 §9); an OTP gets a fresh code,
        stored only as a digest (docs/08 §4)."""
        issued: list[IssuedProbe] = []

        def step(s: Session, ctx: _Context) -> GuardianSessionRecord:
            run = _locked_run(s, probe_run_id, ProbeRunStatus.PENDING)
            if ctx.record.status is not SessionStatus.PLANNING:
                raise _wrong_state(f"Session {ctx.record.id} is {ctx.record.status}.")
            kb = ctx.version.kb
            src = ctx.profile.sources[run.probe_key]
            ui = ui_for(run.probe_key, question=src.question)
            guardrails = kb.config.guardrails
            timeout_s = float(guardrails.active_probe_response_timeout_s)
            if run.probe_key == COOLING_OFF:  # docs/08 §2 step 7: the window is time-scaled
                scale = kb.config.time.demo_time_scale if self._demo_time else 1.0
                timeout_s = (
                    kb.config.classifiers.cooling_off_window_s * scale
                    + guardrails.cooling_off_extra_timeout_s
                )
            now = self._clock.now()
            deadline = now + timedelta(seconds=timeout_s)
            raw: dict[str, Any] = {
                "ui": ui.value,
                "issued_at": now.isoformat(),
                "deadline_at": deadline.isoformat(),
            }
            values: dict[str, Any] = {}
            question = code = None
            if ui is Ui.QUESTION:
                question = serve_question(
                    kb.question_bank[run.probe_key],
                    ctx.language,
                    _last_shown(s, ctx, run.probe_key),
                )
                values = {"variant_id": question.variant_id, "language": question.language}
                raw["options"] = [o.key for o in question.options]
            elif ui is Ui.OTP:
                code = new_otp()
                raw |= {
                    "otp_sha256": otp_digest(self._otp_key, str(probe_run_id), code),
                    "otp_attempts": 0,
                }
            s.execute(
                update(probe_runs)
                .where(probe_runs.c.id == probe_run_id)
                .values(status=ProbeRunStatus.AWAITING_CUSTOMER.value, raw_response=raw, **values)
            )
            current = self._write(s, ctx.record, status=SessionStatus.AWAITING_CUSTOMER.value)
            publish(s, EventType.GUARDIAN_SESSION_UPDATED, str(current.id), _event(current))
            issued.append(
                IssuedProbe(current, probe_run_id, run.probe_key, ui, timeout_s, question, code)
            )
            return current

        current = self._guarded(_session_of(self._sessions, probe_run_id), step)
        return issued[0] if issued else Decided(current)  # else: the fail-safe decided

    def respond(
        self, probe_run_id: UUID, answer: Mapping[str, Any], signals: ClientSignals
    ) -> Responded:
        """Record the customer's answer: classify (docs/08 §5), update, back to PLANNING. A
        wrong OTP with attempts left keeps the run waiting; a cancel in COOLING_OFF ends the
        session (docs/08 §5). Bad answers and implausible timings are the caller's errors."""
        result: list[Responded] = []

        def step(s: Session, ctx: _Context) -> GuardianSessionRecord:
            run = _awaited_run(s, ctx, probe_run_id)
            raw = dict(run.raw_response)
            if self._clock.now() > datetime.fromisoformat(raw["deadline_at"]):
                # docs/08 §5: an OTP passes only before its timeout; no probe takes a late answer
                raise _wrong_state(f"Probe run {probe_run_id} has timed out.")
            ui = Ui(raw["ui"])
            thresholds = ctx.version.kb.config.classifiers
            issued_at = datetime.fromisoformat(raw["issued_at"])
            observed_s = (self._clock.now() - issued_at).total_seconds()
            latency = response_latency_s(signals, observed_s)
            raw |= {"answer": dict(answer), "latency_s": latency, "client_signals": asdict(signals)}
            obs: object
            if ui is Ui.QUESTION:
                obs = question_observation(answer, frozenset(raw["options"]), signals, latency)
            elif ui is Ui.BIOMETRIC:
                obs = biometric_observation(answer)
            elif ui is Ui.COOLING_OFF:
                obs = cooling_off_observation(answer)
            else:
                code = otp_code(answer)
                attempts = int(raw["otp_attempts"]) + 1
                correct = otp_matches(self._otp_key, str(probe_run_id), code, raw["otp_sha256"])
                raw["otp_attempts"] = attempts
                raw["answer"] = {"otp": "******"}  # never keep a code, right or wrong
                left = thresholds.otp_max_attempts - attempts
                if not correct and left > 0:
                    s.execute(
                        update(probe_runs)
                        .where(probe_runs.c.id == probe_run_id)
                        .values(raw_response=raw)
                    )
                    result.append(Responded(ctx.record, False, attempts_remaining=left))
                    return ctx.record
                obs = OtpObs(correct=correct, attempts=attempts, timed_out=False)
            outcome = GUARDIAN_CLASSIFIERS[run.probe_key].classify(obs, thresholds)
            if run.probe_key == COOLING_OFF and outcome == CANCELLED_BY_CUSTOMER:
                s.execute(  # the run completes with its outcome; the session ends, no update
                    update(probe_runs)
                    .where(probe_runs.c.id == probe_run_id)
                    .values(
                        status=ProbeRunStatus.COMPLETED.value,
                        outcome=outcome,
                        raw_response=raw,
                        posterior_after=ctx.as_dict(ctx.belief),
                        info_yield_bits=0.0,
                        completed_at=self._clock.now(),
                    )
                )
                current = self._cancelled(s, ctx)
            else:
                current = self._finish_run(s, ctx, probe_run_id, raw, outcome)
            result.append(Responded(current, True, outcome))
            return current

        current = self._guarded(_session_of(self._sessions, probe_run_id), step)
        return result[0] if result else Responded(current, True)  # else: the fail-safe decided

    def timeout(self, probe_run_id: UUID) -> GuardianSessionRecord:
        """No answer in time (docs/08 §2 step 7): a step-up is classified FAILED_OR_ABANDONED
        and updates the belief; any other probe is TIMED_OUT with no update. Only once its
        deadline has passed."""

        def step(s: Session, ctx: _Context) -> GuardianSessionRecord:
            run = _awaited_run(s, ctx, probe_run_id)
            if self._clock.now() < datetime.fromisoformat(run.raw_response["deadline_at"]):
                raise _wrong_state(f"Probe run {probe_run_id} is still within its deadline.")
            raw = dict(run.raw_response) | {"timed_out": True}
            if run.probe_key in STEP_UPS:
                obs = timed_out_observation(run.probe_key, int(raw.get("otp_attempts", 0)))
                outcome = GUARDIAN_CLASSIFIERS[run.probe_key].classify(
                    obs, ctx.version.kb.config.classifiers
                )
                return self._finish_run(s, ctx, probe_run_id, raw, outcome)
            s.execute(
                update(probe_runs)
                .where(probe_runs.c.id == probe_run_id)
                .values(
                    status=ProbeRunStatus.TIMED_OUT.value,
                    raw_response=raw,
                    posterior_after=ctx.as_dict(ctx.belief),
                    completed_at=self._clock.now(),
                )
            )
            return self._back_to_planning(s, ctx, ctx.belief)

        return self._guarded(_session_of(self._sessions, probe_run_id), step)

    def cancel(self, session_id: UUID) -> GuardianSessionRecord:
        """The customer cancelled (FR-GDN-07): the session ends CANCELLED_BY_CUSTOMER and the
        payment is CANCELLED. A finished session is a caller error."""

        def step(s: Session, ctx: _Context) -> GuardianSessionRecord:
            if ctx.record.status in DONE:
                raise _wrong_state(f"Session {session_id} is already {ctx.record.status}.")
            return self._cancelled(s, ctx)

        return self._guarded(session_id, step)

    def _finish_run(
        self,
        s: Session,
        ctx: _Context,
        probe_run_id: UUID,
        raw: Mapping[str, Any],
        outcome: str,
    ) -> GuardianSessionRecord:
        before = ctx.belief
        after = observe(ctx.profile, before, _run_key(s, probe_run_id), outcome)
        s.execute(
            update(probe_runs)
            .where(probe_runs.c.id == probe_run_id)
            .values(
                status=ProbeRunStatus.COMPLETED.value,
                outcome=outcome,
                raw_response=dict(raw),
                posterior_after=ctx.as_dict(after),
                info_yield_bits=kl_bits(after, before),
                completed_at=self._clock.now(),
            )
        )
        return self._back_to_planning(s, ctx, after)

    def _back_to_planning(
        self, s: Session, ctx: _Context, belief: np.ndarray
    ) -> GuardianSessionRecord:
        current = self._write(
            s,
            ctx.record,
            status=SessionStatus.PLANNING.value,
            posterior=ctx.as_dict(belief),
            risk_by_decision=_risks(ctx.profile, risk_by_decision(belief, ctx.losses())),
        )
        publish(s, EventType.GUARDIAN_SESSION_UPDATED, str(current.id), _event(current))
        return current

    def _cancelled(self, s: Session, ctx: _Context) -> GuardianSessionRecord:
        """The session ends CANCELLED_BY_CUSTOMER: its open run is closed (``closed_by``
        CUSTOMER_CANCELLED), the payment CANCELLED. Written against the version this step
        loaded, so a concurrent decision makes the cancel fail rather than overwrite it."""
        now = self._clock.now()
        _close_open_runs(s, ctx.record.id, TerminalReason.CUSTOMER_CANCELLED.value, now)
        bank.cancel_payment(s, ctx.payment.id, now)
        current = self._write(
            s,
            ctx.record,
            status=SessionStatus.CANCELLED_BY_CUSTOMER.value,
            terminal_reason=TerminalReason.CUSTOMER_CANCELLED.value,
            decided_at=now,
        )
        record(
            s,
            self._clock,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="guardian.session.cancelled",
            entity_type="guardian_session",
            entity_id=str(current.id),
            payload={"payment_id": str(ctx.payment.id)},
        )
        publish(s, EventType.GUARDIAN_SESSION_UPDATED, str(current.id), _event(current))
        return current

    # ------------------------------------------------------------------ the decision

    def _decide(
        self,
        s: Session,
        ctx: _Context,
        belief: np.ndarray,
        reason: TerminalReason,
        *,
        decision: str | None = None,
        plan: PlanResult | None = None,
    ) -> GuardianSessionRecord:
        """Step 9: d*(posterior) (or the fail-safe's decision), the payment's status, a case for
        a hold, the Scout flag, the audit entry and the event."""
        kb = ctx.version.kb
        risks: dict[str, float] | None
        if decision is None:
            decision, risks = final_decision(
                ctx.profile,
                belief,
                amount_paise=ctx.payment.amount_paise,
                vulnerable=ctx.vulnerable,
            )
        else:
            risks = ctx.stored_risks
        posterior = ctx.as_dict(belief)
        unexplained = (
            posterior.get(UNEXPLAINED, 0.0) >= kb.config.engine.unexplained_routing_threshold
        )
        now = self._clock.now()
        _close_open_runs(s, ctx.record.id, reason.value, now)
        payment_status = bank.apply_guardian_decision(s, ctx.payment.id, decision, now)
        case_id = None
        if decision in navigator.HOLD_DECISIONS:
            case = navigator.open_case(
                s,
                self._clock,
                decision=decision,
                payment_id=ctx.payment.id,
                guardian_session_id=ctx.record.id,
                kb_version_id=ctx.record.kb_version_id,
                posterior=posterior,
                prior_components=ctx.prior_components,
                amount_at_stake_paise=ctx.payment.amount_paise,
                unexplained=unexplained,
                demo_time=self._demo_time,
            )
            case_id = case.id
        decided = self._write(
            s,
            ctx.record,
            status=SessionStatus.DECIDED.value,
            decision=decision,
            terminal_reason=reason.value,
            decided_at=now,
            posterior=posterior,
            risk_by_decision=risks,
            unexplained=unexplained,
            case_id=case_id,
        )
        payload: dict[str, Any] = {
            "payment_id": str(ctx.payment.id),
            "decision": decision,
            "terminal_reason": reason.value,
            "payment_status": payment_status.value,
            "case_id": str(case_id) if case_id else None,
            "unexplained": unexplained,
        }
        if plan is not None:  # the last candidates table, for the DD pack (docs/08 §2 step 5)
            payload["final_candidates"] = _candidates(plan)
        record(
            s,
            self._clock,
            actor_type=ActorType.ENGINE,
            actor_id=None,
            action="guardian.session.decided",
            entity_type="guardian_session",
            entity_id=str(ctx.record.id),
            payload=payload,
        )
        # docs/09 §7: the flag is recomputed on every change; a decision is the last one. The
        # check never blocks the decision: if building the pack fails, the flag stays false.
        if self._dd_pack_complete(s, decided.id):
            decided = self._write(s, decided, dd_pack_complete=True)
        publish(s, EventType.GUARDIAN_SESSION_DECIDED, str(ctx.record.id), _event(decided))
        return decided

    def _dd_pack_complete(self, s: Session, session_id: UUID) -> bool:
        try:
            with s.begin_nested():  # a failed read must not abort the decision's transaction
                return ddpack.build(s, session_id).complete
        except Exception:
            log.exception("guardian.dd_pack_check_failed", session_id=str(session_id))
            return False

    # ------------------------------------------------------------------ plumbing

    def _guarded(
        self, session_id: UUID, step: Callable[[Session, _Context], GuardianSessionRecord]
    ) -> GuardianSessionRecord:
        """Run a step in its own transaction; any error rolls it back and takes the fail-safe."""
        try:
            with self._sessions.begin() as s:
                return step(s, self._load(s, session_id))
        except DelphiError as exc:
            if exc.code in CALLER_ERRORS:
                raise  # a caller's mistake (a bad answer, a wrong state), not an engine error
            return self.fail_safe(session_id, exc)
        except Exception as exc:
            return self.fail_safe(session_id, exc)

    def _profile(self, version: StoredVersion) -> CompiledProfile:
        compiled = self._profiles.get(version.id)
        if compiled is None:
            compiled = compile_profile(version.kb, PROFILE)
            self._profiles[version.id] = compiled
        return compiled

    def _load(self, s: Session, session_id: UUID) -> _Context:
        current = get_session(s, session_id)
        if current is None:
            raise NotFoundError(f"No Guardian session {session_id}.")
        version = get_version(s, current.kb_version_id)
        if version is None:
            raise NotFoundError(f"No KB version {current.kb_version_id}.")
        payment = bank.get_payment(s, current.payment_id)
        if payment is None:
            raise NotFoundError(f"No payment {current.payment_id}.")
        row = s.execute(
            select(
                guardian_sessions.c.prior,
                guardian_sessions.c.p_model,
                guardian_sessions.c.anomaly_percentile,
                guardian_sessions.c.open_mass,
                guardian_sessions.c.open_mass_shares,
                guardian_sessions.c.risk_by_decision,
            ).where(guardian_sessions.c.id == session_id)
        ).one()
        customer = s.execute(
            select(
                bank.customers.c.language,
                bank.customers.c.is_vulnerable,
                bank.customers.c.consent_call_status,
            ).where(bank.customers.c.id == _require(payment.payer_customer_id))
        ).one()
        consents = frozenset({CALL_STATUS_CONSENT}) if customer.consent_call_status else frozenset()
        ctx = _Context(
            record=current,
            version=version,
            profile=self._profile(version),
            payment=payment,
            language=customer.language,
            vulnerable=bool(customer.is_vulnerable),
            consents=consents,
            prior_components={
                "prior": row.prior,
                "p_model": row.p_model,
                "anomaly_percentile": row.anomaly_percentile,
                "open_mass": row.open_mass,
                "open_mass_shares": row.open_mass_shares,
            },
            stored_risks=row.risk_by_decision,
        )
        ctx.belief = _belief(ctx.profile, current.posterior or row.prior)
        return ctx

    def _facts(self, s: Session, ctx: _Context) -> SessionFacts:
        """The session-fixed guardrails (docs/06 §8): channel, consent, language, fairness."""
        bank_ = ctx.version.kb.question_bank
        servable = frozenset(
            key
            for key, entry in bank_.items()
            if approved_variants(entry, ctx.language) or approved_variants(entry, FALLBACK_LANGUAGE)
        )
        cap = ctx.version.kb.config.guardrails.max_active_sessions_per_customer_30d
        return SessionFacts(
            channel=ctx.payment.channel,
            consents=ctx.consents,
            servable_question_probes=servable,
            fairness_cap_reached=_earlier_active_sessions(s, ctx, self._clock) >= cap,
        )

    def _write(
        self, s: Session, current: GuardianSessionRecord, **values: Any
    ) -> GuardianSessionRecord:
        """Update the session row, guarded by its version (docs/05 §2: one writer at a time)."""
        gs = guardian_sessions.c
        done = s.execute(
            update(guardian_sessions)
            .where(gs.id == current.id, gs.version == current.version)
            .values(**values, version=current.version + 1)
            .returning(gs.id)
        ).scalar_one_or_none()
        if done is None:
            raise DelphiError(
                f"Guardian session {current.id} changed underneath this step.",
                code=ProblemCode.INVALID_STATE_TRANSITION,
            )
        return _reload(s, current.id)


# ---------------------------------------------------------------------- helpers


def _reload(conn: Connection | Session, session_id: UUID) -> GuardianSessionRecord:
    current = get_session(conn, session_id)
    if current is None:
        raise NotFoundError(f"No Guardian session {session_id}.")
    return current


def _require[T](value: T | None) -> T:
    if value is None:
        raise DelphiError(
            "Only a customer's payment has a Guardian session.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    return value


def _belief(profile: CompiledProfile, stored: Mapping[str, float] | None) -> np.ndarray:
    """The stored distribution as a vector over the profile's hypotheses; uniform if none was
    stored yet (only the fail-safe meets that: the prior was never built)."""
    n = len(profile.hypotheses)
    if not stored:
        return np.full(n, 1.0 / n)
    return np.array([float(stored.get(h, 0.0)) for h in profile.hypotheses], dtype=np.float64)


def _risks(profile: CompiledProfile, risks: np.ndarray) -> dict[str, float]:
    return {d: float(r) for d, r in zip(profile.decisions, risks, strict=True)}


def _paise(value: float | None) -> int | None:
    return None if value is None else round(value)


def _candidates(plan: PlanResult) -> list[dict[str, Any]]:
    """docs/08 §6: the candidates table as persisted and shown by the monitor."""
    return [
        {
            "source_key": c.source_key,
            "eligible": c.eligible,
            "ineligible_reason": c.ineligible_reason.value if c.ineligible_reason else None,
            "cost_paise": _paise(c.cost_paise),
            "voi_myopic_paise": _paise(c.voi_myopic_paise),
            "net_value_paise": _paise(c.net_value_paise),
            "settle_pct": c.settle_pct,
            "selected": c.source_key == plan.action,
        }
        for c in plan.candidates
    ]


def _event(current: GuardianSessionRecord) -> dict[str, Any]:
    """SSE payloads stay small; clients re-fetch the session (docs/16 §3)."""
    return {
        "session_id": str(current.id),
        "payment_id": str(current.payment_id),
        "status": current.status.value,
        "decision": current.decision,
    }


def _earlier_active_sessions(s: Session, ctx: _Context, clock: Clock) -> int:
    """The payer's other sessions in the last 30 days with at least one active probe
    (the fairness cap, docs/06 §8)."""
    gs, pr, p, a = guardian_sessions.c, probe_runs.c, bank.payments.c, bank.accounts.c
    customer = _require(ctx.payment.payer_customer_id)
    return int(
        s.execute(
            select(func.count(distinct(gs.id)))
            .select_from(
                guardian_sessions.join(probe_runs, pr.session_id == gs.id)
                .join(bank.payments, p.id == gs.payment_id)
                .join(bank.accounts, a.id == p.payer_account_id)
            )
            .where(
                a.customer_id == customer,
                gs.id != ctx.record.id,
                gs.started_at >= clock.now() - FAIRNESS_WINDOW,
                pr.mode == ProbeMode.ACTIVE.value,
                pr.status != ProbeRunStatus.PENDING.value,  # chosen but never shown
            )
        ).scalar_one()
    )


def _wrong_state(message: str) -> DelphiError:
    return DelphiError(message, code=ProblemCode.INVALID_STATE_TRANSITION)


def _session_of(sessions: sessionmaker[Session], probe_run_id: UUID) -> UUID:
    with sessions() as s:
        session_id = s.execute(
            select(probe_runs.c.session_id).where(probe_runs.c.id == probe_run_id)
        ).scalar_one_or_none()
    if session_id is None:
        raise NotFoundError(f"No probe run {probe_run_id}.")
    return UUID(str(session_id))


def _locked_run(s: Session, probe_run_id: UUID, expected: ProbeRunStatus) -> Any:
    """The active probe run, locked, in the status the step needs."""
    run = s.execute(
        select(probe_runs).where(probe_runs.c.id == probe_run_id).with_for_update()
    ).one()
    if run.mode != ProbeMode.ACTIVE.value or run.status != expected.value:
        raise _wrong_state(f"Probe run {probe_run_id} is {run.status}, not {expected}.")
    return run


def _awaited_run(s: Session, ctx: _Context, probe_run_id: UUID) -> Any:
    """The run, locked and AWAITING_CUSTOMER, in a session that is waiting for it. A run left
    open by a finished session is never answered or timed out (docs/05 §2)."""
    run = _locked_run(s, probe_run_id, ProbeRunStatus.AWAITING_CUSTOMER)
    if ctx.record.status is not SessionStatus.AWAITING_CUSTOMER:
        raise _wrong_state(f"Session {ctx.record.id} is {ctx.record.status}.")
    return run


def _run_key(s: Session, probe_run_id: UUID) -> str:
    return str(
        s.execute(
            select(probe_runs.c.probe_key).where(probe_runs.c.id == probe_run_id)
        ).scalar_one()
    )


def _last_shown(s: Session, ctx: _Context, probe_key: str) -> dict[str, datetime]:
    """When the payer was last shown each variant of ``probe_key``, over all their sessions
    (docs/07 §9: rotation per customer)."""
    gs, pr, p, a = guardian_sessions.c, probe_runs.c, bank.payments.c, bank.accounts.c
    rows = s.execute(
        select(pr.variant_id, func.max(pr.started_at))
        .select_from(
            probe_runs.join(guardian_sessions, gs.id == pr.session_id)
            .join(bank.payments, p.id == gs.payment_id)
            .join(bank.accounts, a.id == p.payer_account_id)
        )
        .where(
            a.customer_id == _require(ctx.payment.payer_customer_id),
            pr.probe_key == probe_key,
            pr.variant_id.is_not(None),
        )
        .group_by(pr.variant_id)
    ).all()
    return {variant: shown for variant, shown in rows}


COOLING_OFF_KEY = "COOLING_OFF"


def _session_age(s: Session, current: GuardianSessionRecord, now: datetime) -> timedelta:
    """The session's age for ``session_max_duration_s``, without the COOLING_OFF window
    (docs/08 §2 step 7: that window does not count)."""
    pr = probe_runs.c
    windows = s.execute(
        select(pr.raw_response, pr.started_at, pr.completed_at).where(
            pr.session_id == current.id,
            pr.probe_key == COOLING_OFF_KEY,
            pr.status != ProbeRunStatus.PENDING.value,
        )
    ).all()
    excluded = timedelta()
    for raw, started, completed in windows:
        issued = datetime.fromisoformat(raw["issued_at"]) if raw and "issued_at" in raw else started
        excluded += (completed or now) - issued
    return now - current.started_at - excluded


def _close_open_runs(s: Session, session_id: UUID, reason: str, now: datetime) -> None:
    """A finished session has no open probe run: one left PENDING or AWAITING_CUSTOMER when the
    session ends becomes ERROR, marked with why (docs/05 §2)."""
    s.execute(
        update(probe_runs)
        .where(
            probe_runs.c.session_id == session_id,
            probe_runs.c.status.in_(
                [ProbeRunStatus.PENDING.value, ProbeRunStatus.AWAITING_CUSTOMER.value]
            ),
        )
        .values(
            status=ProbeRunStatus.ERROR.value,
            raw_response={"closed_by": reason},
            completed_at=now,
        )
    )
