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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

import numpy as np
from sqlalchemy import Connection, distinct, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.engine.belief import kl_bits
from delphi.engine.belief import update as bayes_update
from delphi.engine.compile import compile_profile
from delphi.engine.exploration import session_seed
from delphi.engine.planner import (
    SessionFacts,
    initial_budgets,
    plan_next,
    static_ineligibility,
)
from delphi.engine.prior import build_prior, is_fast_path, open_mass
from delphi.engine.risk import decide, loss_matrix, risk_by_decision, voi_myopic
from delphi.engine.types import CompiledProfile, Phase, PlanResult, StopReason
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
    Fetcher,
    PassiveRequest,
    PassiveStatus,
    approved_variants,
    run_passive_probes,
)
from delphi.sensing.api import with_payer_mule
from delphi.sim import api as bank

PROFILE = "GUARDIAN_PAYMENT"
FALLBACK_LANGUAGE = "en"
CALL_STATUS_CONSENT = "call_status"  # kb/sources/probes/CALL_STATUS.yaml requires_consent
FAIRNESS_WINDOW = timedelta(days=30)  # docs/06 §8: the rolling 30 days of the fairness cap
DONE = frozenset({SessionStatus.DECIDED, SessionStatus.CANCELLED_BY_CUSTOMER})

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PriorInputs:
    """What the sensing layer says about a payment (docs/13 §1)."""

    p_payment: Mapping[str, float]  # the payment model's classes (no MULE, docs/13 §4)
    anomaly_percentile: float  # q
    p_mule_payer: float | None  # the payer account's mule score; None without a scorer
    top_features: tuple[Mapping[str, Any], ...] = ()


class PriorPort(Protocol):
    """The sensing inference (#92). Reads only data from before the payment's initiation."""

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
    plan: PlanResult


PlanOutcome = Decided | NextProbe


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
        demo_time: bool = False,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._prior = prior
        self._fetchers = fetchers
        self._executor = executor
        self._demo_time = demo_time
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
        p_model = with_payer_mule(inputs.p_payment, inputs.p_mule_payer)
        q = float(inputs.anomaly_percentile)
        profile = ctx.profile
        prior = build_prior(profile, p_model, q)
        u = open_mass(q, profile.open_mass_base, profile.open_mass_slope)
        kb_only = [h for h in profile.hypotheses if h not in p_model and h != UNEXPLAINED]
        shares = {h: profile.open_mass_shares.get(h, 0.0) for h in kb_only}
        risks = risk_by_decision(prior, ctx.losses())
        self._write(
            s,
            ctx.record,
            prior=ctx.as_dict(prior),
            p_model=dict(p_model),
            anomaly_percentile=q,
            open_mass=u,
            open_mass_shares=shares,
            top_features=[dict(f) for f in inputs.top_features],
            posterior=ctx.as_dict(prior),
            risk_by_decision=_risks(profile, risks),
        )
        fast = ctx.version.kb.config.engine.fast_path
        if is_fast_path(
            float(p_model.get("GENUINE", 0.0)),
            float(ctx.payment.amount_paise),
            q,
            min_p_genuine=fast.min_p_genuine,
            max_amount_inr=fast.max_amount_inr,
            max_anomaly_percentile=fast.max_anomaly_percentile,
        ):
            return self._decide(s, self._load(s, ctx.record.id), prior, TerminalReason.FAST_PATH)
        return _reload(s, ctx.record.id)

    async def passive(self, session_id: UUID) -> GuardianSessionRecord:
        """Step 4: every eligible passive probe in parallel, each with the KB's adapter timeout.
        A timeout or adapter error is an ERROR run with no update (FR-GDN-03); updates apply in
        KB source order. Only a CREATED session is probed, so a resumed call is harmless."""
        with self._sessions.begin() as s:
            ctx = self._load(s, session_id)
            if ctx.record.status is not SessionStatus.CREATED:
                return ctx.record
            self._write(s, ctx.record, status=SessionStatus.PASSIVE_PROBING.value)
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
                    after = bayes_update(belief, src.likelihood[src.outcomes.index(result.outcome)])
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
                _reload(s, session_id),
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
            if ctx.record.status is not SessionStatus.PLANNING:
                raise DelphiError(
                    f"Session {session_id} is {ctx.record.status}, not PLANNING.",
                    code=ProblemCode.INVALID_STATE_TRANSITION,
                )
            guardrails = ctx.version.kb.config.guardrails
            if self._clock.now() - ctx.record.started_at > timedelta(
                seconds=guardrails.session_max_duration_s
            ):
                decided = self._decide(s, ctx, ctx.belief, TerminalReason.SESSION_TIMEOUT)
                outcome.append(Decided(decided))
                return decided
            runs = list_probe_runs(s, session_id)
            ran = tuple(r.probe_key for r in runs)
            active = sum(1 for r in runs if r.mode is ProbeMode.ACTIVE)
            plan = plan_next(
                ctx.profile,
                ctx.belief,
                amount_paise=float(ctx.payment.amount_paise),
                vulnerable=ctx.vulnerable,
                phase=Phase.GUARDIAN_ACTIVE,
                observed=frozenset(ran),
                facts=self._facts(s, ctx),
                budgets=initial_budgets(ctx.profile, ran),
                # One stream per planning step, derived from the session's seed (docs/06 §10),
                # so a resumed or replayed session draws the same numbers.
                rng=np.random.default_rng([ctx.record.rng_seed, active]),
            )
            if plan.is_stop:
                reason = (
                    TerminalReason.STOP
                    if plan.stop_reason is StopReason.NO_POSITIVE_VALUE
                    else TerminalReason.BUDGET_EXHAUSTED
                )
                decided = self._decide(s, ctx, ctx.belief, reason, plan=plan)
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
        with self._sessions.begin() as s:
            current = _reload(s, session_id)
            if current.status in DONE:
                return current
            ctx = self._load(s, session_id)
            return self._decide(
                s, ctx, ctx.belief, TerminalReason.ENGINE_ERROR, decision="HOLD_FOR_REVIEW"
            )

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
        if decision is None:
            idx, _ = decide(belief, ctx.losses())
            decision = ctx.profile.decisions[idx]
            risks: dict[str, float] | None = _risks(
                ctx.profile, risk_by_decision(belief, ctx.losses())
            )
        else:
            risks = ctx.stored_risks
        posterior = ctx.as_dict(belief)
        unexplained = (
            posterior.get(UNEXPLAINED, 0.0) >= kb.config.engine.unexplained_routing_threshold
        )
        now = self._clock.now()
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
        publish(s, EventType.GUARDIAN_SESSION_DECIDED, str(ctx.record.id), _event(decided))
        return decided

    # ------------------------------------------------------------------ plumbing

    def _guarded(
        self, session_id: UUID, step: Callable[[Session, _Context], GuardianSessionRecord]
    ) -> GuardianSessionRecord:
        """Run a step in its own transaction; any error rolls it back and takes the fail-safe."""
        try:
            with self._sessions.begin() as s:
                return step(s, self._load(s, session_id))
        except DelphiError as exc:
            if exc.code is ProblemCode.INVALID_STATE_TRANSITION:
                raise  # a caller's mistake (e.g. planning a decided session), not an engine error
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
            )
        ).scalar_one()
    )
