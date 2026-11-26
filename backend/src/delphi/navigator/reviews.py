"""ACCOUNT_REVIEW cases from complaints and mule scores (docs/09 §1, FR-NAV-01, #109).

A complaint that names a home-bank counterparty account opens an ACCOUNT_REVIEW case for it,
unless the account already has one open. The initial posterior is the mule-scorer prior
(docs/06 §11: p_model(MULE) = p_mule, p_model(GENUINE) = 1 − p_mule), updated with
``LINKED_COMPLAINTS = ONE_OR_MORE``. That update is stored as the case's first evidence record,
so the console shows it in the timeline and never plans it again.

``p_mule`` is the account's stored score (``account_scores``, written by the mule scorer, #91).
Until an account has one, the docs/13 §6 fallback is used and logged. The account review has no
anomaly percentile of its own, so q = 0 and the open mass is the profile's base.

The mule-scorer trigger (``open_mule_reviews``) opens a review for a home-bank account whose
stored score (``MULE_SWEEP``, #91) is at least ``navigator.mule_sweep_threshold`` and that was
credited in the last ``navigator.mule_credit_window_h``, with the mule-scorer prior alone.

``ACCOUNT_REVIEW_SWEEP`` runs both every minute (docs/04 §5.4), the complaints first. Each is
idempotent per account: a case opens only for a complaint, or a credit, newer than the account's
latest review, so a disposed review is not reopened by what opened it.

It is bounded (the 2026-11-22 incident, #263): the candidates are chosen in SQL, oldest
complaint (or credit) first, with ``NOT EXISTS`` against the account's reviews and a ``LIMIT``;
only complaints reported in the last ``review_complaint_lookback_days`` count; each batch of
``review_sweep_batch`` reviews commits on its own, under a statement timeout and a small
``work_mem``; and a run stops after ``review_sweep_max_per_run`` reviews of either trigger, for
the next run to carry on. A run that finds another one sweeping (the advisory lock) skips.

One bad account cannot block the rest (#290): each candidate opens in a savepoint. If its open
fails, the savepoint rolls back, the failure is recorded in ``review_sweep_failures`` (attempts,
last error, next attempt), and the batch commits the others. The candidate is skipped until its
next attempt, ``review_retry_base_s`` after the first failure, doubling with each failure up to
``review_retry_max_s``. A successful open deletes the record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, column, exists, func, or_, select, text, true
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from delphi.engine.prior import build_prior, open_mass
from delphi.kb.model import UNEXPLAINED
from delphi.kb.store import active_version, get_version
from delphi.navigator.console import case_profile, recommend
from delphi.navigator.evidence import request_evidence
from delphi.navigator.store import (
    CaseExistsError,
    CaseProtocol,
    CaseRecord,
    CaseStatus,
    CaseType,
    get_case,
)
from delphi.navigator.tables import cases, review_sweep_failures
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobContext
from delphi.platform.locks import ACCOUNT_REVIEW_SWEEP_LOCK_KEY
from delphi.platform.logging import get_logger
from delphi.platform.settings import Settings
from delphi.sim import api as bank

log = get_logger(__name__)

LINKED_COMPLAINTS = "LINKED_COMPLAINTS"
MULE = "MULE"
GENUINE = "GENUINE"
EXECUTED = "EXECUTED"
CREDITS_WINDOW = timedelta(days=7)  # docs/02: an account's amount at stake is its 7-day credits
NO_ANOMALY = 0.0  # docs/06 §11: no account anomaly model, so the open mass is the base
# Limits for each sweep transaction (docs/23: postgres runs in 1 GiB), as LABEL_DELIVERY's.
STATEMENT_TIMEOUT = "30s"
WORK_MEM = "16MB"
MAX_ERROR_CHARS = 500  # of the last error kept with a failed candidate
MAX_DOUBLINGS = 32  # keeps the backoff's multiplier finite; the cap applies long before


@dataclass(frozen=True, slots=True)
class Backoff:
    """How long a candidate whose review failed to open waits (settings ``review_retry_*``)."""

    base: timedelta  # after the first failure
    cap: timedelta  # the longest wait

    @staticmethod
    def of(settings: Settings) -> Backoff:
        return Backoff(
            base=timedelta(seconds=settings.review_retry_base_s),
            cap=timedelta(seconds=settings.review_retry_max_s),
        )

    def after(self, attempts: int) -> timedelta:
        """The wait after ``attempts`` failed attempts: base, doubling each time, at most cap."""
        return min(self.base * (1 << min(attempts - 1, MAX_DOUBLINGS)), self.cap)


@dataclass(frozen=True, slots=True)
class Swept:
    """One trigger's part of a batch: the reviews it opened and the accounts that failed."""

    opened: list[CaseRecord]
    failed: list[UUID]


@dataclass(frozen=True, slots=True)
class SweepLimits:
    """How much one sweep does (settings ``review_*``)."""

    batch: int  # candidates tried per transaction
    max_per_run: int  # candidates tried per run
    complaint_lookback: timedelta  # only complaints reported this recently open a review
    backoff: Backoff  # for a candidate whose review failed to open

    @staticmethod
    def of(settings: Settings) -> SweepLimits:
        return SweepLimits(
            batch=settings.review_sweep_batch,
            max_per_run=settings.review_sweep_max_per_run,
            complaint_lookback=timedelta(days=settings.review_complaint_lookback_days),
            backoff=Backoff.of(settings),
        )


def open_review_for_account(session: Session, account_id: UUID) -> CaseRecord | None:
    row = session.execute(
        select(cases).where(
            cases.c.subject_account_id == account_id,
            cases.c.type == CaseType.ACCOUNT_REVIEW.value,
            cases.c.status != CaseStatus.DISPOSED.value,
        )
    ).one_or_none()
    return CaseRecord.of(row) if row is not None else None


def stored_p_mule(session: Session, account_id: UUID) -> float | None:
    s = bank.account_scores.c
    value = session.execute(select(s.p_mule).where(s.account_id == account_id)).scalar()
    return None if value is None else float(value)


def credits_7d_paise(session: Session, account_id: UUID, now: datetime) -> int:
    p = bank.payments.c
    total = session.execute(
        select(func.coalesce(func.sum(p.amount_paise), 0).cast(BigInteger)).where(
            p.payee_account_id == account_id,
            p.status == EXECUTED,
            p.executed_at > now - CREDITS_WINDOW,
            p.executed_at <= now,
        )
    ).scalar_one()
    return int(total)


def open_account_review(
    session: Session,
    clock: Clock,
    *,
    account_id: UUID,
    kb_version_id: int,
    p_mule: float,
    p_mule_source: str,
    demo_time: bool = False,
) -> CaseRecord:
    """An ACCOUNT_REVIEW case (protocol STANDARD) with the mule-scorer prior, in the caller's
    transaction. Refused (``CaseExistsError``) if the account already has an open review."""
    if open_review_for_account(session, account_id) is not None:
        raise CaseExistsError(f"Account {account_id} already has an open review.")
    version = get_version(session, kb_version_id)
    if version is None:
        raise DelphiError(f"No KB version {kb_version_id}.", code=ProblemCode.VALIDATION_FAILED)
    kb = version.kb
    profile = case_profile(kb, CaseType.ACCOUNT_REVIEW.value)
    p_model = {MULE: p_mule, GENUINE: 1.0 - p_mule}
    prior = build_prior(profile, p_model, NO_ANOMALY)
    posterior = {h: float(p) for h, p in zip(profile.hypotheses, prior, strict=True)}
    components: dict[str, Any] = {
        "p_model": p_model,
        "p_mule_source": p_mule_source,
        "anomaly_percentile": NO_ANOMALY,
        "open_mass": open_mass(NO_ANOMALY, profile.open_mass_base, profile.open_mass_slope),
    }
    order = [*kb.hypothesis_order, UNEXPLAINED]  # ties: KB order, UNEXPLAINED last
    leading = min(posterior, key=lambda h: (-posterior[h], order.index(h)))
    now = clock.now()
    scale = kb.config.time.demo_time_scale if demo_time else 1.0
    sla_due_at = now + timedelta(minutes=kb.regulatory.case_sla.account_review_sla_minutes * scale)
    case_id = uuid7(unix_ms=int(now.timestamp() * 1000))
    amount = credits_7d_paise(session, account_id, now)
    # #208: the recommendation is stored from the start. The trigger's evidence record, taken
    # straight after, recomputes it with the account owner's vulnerability.
    advice = recommend(profile, posterior, amount_paise=amount, vulnerable=False)
    try:
        with session.begin_nested():
            session.execute(
                cases.insert().values(
                    id=case_id,
                    type=CaseType.ACCOUNT_REVIEW.value,
                    protocol=CaseProtocol.STANDARD.value,
                    status=CaseStatus.OPEN.value,
                    subject_account_id=account_id,
                    kb_version_id=kb_version_id,
                    prior_components=components,
                    posterior=posterior,
                    leading_hypothesis=leading,
                    amount_at_stake_paise=amount,
                    recommended_decision=advice.decision,
                    risk_by_decision=dict(advice.risk_by_decision),
                    opened_at=now,
                    sla_due_at=sla_due_at,
                    is_override=False,
                    dd_pack_complete=False,
                    unexplained=leading == UNEXPLAINED,
                    version=1,
                )
            )
    except IntegrityError:  # a concurrent open won the race (the partial unique, docs/17)
        raise CaseExistsError(f"Account {account_id} already has an open review.") from None
    payload = {
        "type": CaseType.ACCOUNT_REVIEW.value,
        "protocol": CaseProtocol.STANDARD.value,
        "account_id": str(account_id),
        "leading_hypothesis": leading,
        "sla_due_at": sla_due_at.isoformat(),
    }
    record(
        session,
        clock,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        action="case.opened",
        entity_type="case",
        entity_id=str(case_id),
        payload=payload,
    )
    publish(session, EventType.CASE_OPENED, str(case_id), payload)
    opened = get_case(session, case_id)
    assert opened is not None
    return opened


def backing_off(account_id: Any, now: datetime) -> Any:
    """True while the account's last failed open (#290) waits for its next attempt."""
    f = review_sweep_failures.c
    return exists().where(f.account_id == account_id, f.next_attempt_at > now)


def record_failure(
    session: Session, clock: Clock, account_id: UUID, error: BaseException, backoff: Backoff
) -> None:
    """Count a failed open of the account's review and set its next attempt (#290)."""
    f = review_sweep_failures.c
    now = clock.now()
    attempts = 1 + (
        session.execute(
            select(f.attempts).where(f.account_id == account_id).with_for_update()
        ).scalar()
        or 0
    )
    next_attempt_at = now + backoff.after(attempts)
    last_error = f"{type(error).__name__}: {error}"[:MAX_ERROR_CHARS]
    session.execute(
        insert(review_sweep_failures)
        .values(
            account_id=account_id,
            attempts=attempts,
            last_error=last_error,
            next_attempt_at=next_attempt_at,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[f.account_id],
            set_={
                "attempts": attempts,
                "last_error": last_error,
                "next_attempt_at": next_attempt_at,
                "updated_at": now,
            },
        )
    )
    log.warning(
        "navigator.review_open_failed",
        account_id=str(account_id),
        attempts=attempts,
        next_attempt_at=next_attempt_at.isoformat(),
        error=last_error,
    )


def complaint_candidates(
    session: Session, now: datetime, *, since: datetime, limit: int
) -> list[tuple[UUID, datetime]]:
    """Home-bank accounts named by a complaint reported in ``(since, now]`` that has no review
    since (none open, none opened at or after the complaint), oldest complaint first, at most
    ``limit`` (account, complaint time) pairs, each account once."""
    c, a, k = bank.complaints.c, bank.accounts.c, cases.c
    named = (
        func.unnest(c.counterparty_account_ids)
        .table_valued(column("account_id", PG_UUID(as_uuid=True)))
        .render_derived(name="named")
        .lateral()
    )
    account_id = named.c.account_id
    reviewed = exists().where(
        k.subject_account_id == account_id,
        k.type == CaseType.ACCOUNT_REVIEW.value,
        or_(k.status != CaseStatus.DISPOSED.value, k.opened_at >= c.reported_at),
    )
    rows = session.execute(
        select(account_id, c.reported_at)
        .select_from(bank.complaints.join(named, true()).join(bank.accounts, a.id == account_id))
        .where(
            a.bank_code == bank.HOME_BANK,
            c.reported_at > since,
            c.reported_at <= now,
            ~reviewed,
            ~backing_off(account_id, now),
        )
        .order_by(c.reported_at, c.id, account_id)
        .limit(limit)
    ).all()
    seen: dict[UUID, datetime] = {}
    for named_id, reported_at in rows:
        seen.setdefault(named_id, reported_at)
    return list(seen.items())


def open_with_trigger(
    session: Session,
    clock: Clock,
    account_id: UUID,
    trigger: str | None,
    *,
    kb_version_id: int,
    p_mule_fallback: float | None = None,
    demo_time: bool,
) -> CaseRecord | None:
    """Open the account's review, with the ``trigger`` probe as its first evidence record (none
    for the mule-scorer trigger); None if a review is already open (a concurrent open won).
    ``p_mule_fallback`` is needed only when the account may have no stored score (complaints);
    a mule suspect always has one."""
    p_mule = stored_p_mule(session, account_id)
    source = "account_scores"
    if p_mule is None:
        if p_mule_fallback is None:
            raise DelphiError(
                f"Account {account_id} has no stored mule score and no fallback was given.",
                code=ProblemCode.VALIDATION_FAILED,
            )
        log.warning("navigator.p_mule_fallback", account_id=str(account_id))
        p_mule, source = p_mule_fallback, "fallback"
    try:
        case = open_account_review(
            session,
            clock,
            account_id=account_id,
            kb_version_id=kb_version_id,
            p_mule=p_mule,
            p_mule_source=source,
            demo_time=demo_time,
        )
    except CaseExistsError:
        return None
    if trigger is None:
        return case
    request_evidence(session, clock, case.id, trigger, requested_by=None, on_open=True)
    refreshed = get_case(session, case.id)
    assert refreshed is not None
    return refreshed


def try_open(
    session: Session,
    clock: Clock,
    account_id: UUID,
    trigger: str | None,
    swept: Swept,
    *,
    backoff: Backoff,
    kb_version_id: int,
    p_mule_fallback: float | None = None,
    demo_time: bool,
) -> None:
    """Open the account's review in a savepoint (``open_with_trigger``). On a failure, roll back
    only this account, record it and back it off, so the caller's batch commits the others
    (#290)."""
    try:
        with session.begin_nested():
            case = open_with_trigger(
                session,
                clock,
                account_id,
                trigger,
                kb_version_id=kb_version_id,
                p_mule_fallback=p_mule_fallback,
                demo_time=demo_time,
            )
    except Exception as error:  # any failure: one account must not roll back the batch
        record_failure(session, clock, account_id, error, backoff)
        swept.failed.append(account_id)
        return
    session.execute(
        review_sweep_failures.delete().where(review_sweep_failures.c.account_id == account_id)
    )
    if case is not None:
        swept.opened.append(case)


def open_complaint_reviews(
    session: Session,
    clock: Clock,
    *,
    kb_version_id: int,
    p_mule_fallback: float,
    since: datetime,
    limit: int,
    backoff: Backoff,
    demo_time: bool = False,
) -> Swept:
    """docs/09 §1: try up to ``limit`` candidates, one for each home-bank account a complaint
    reported after ``since`` names, unless it has an open review, or a review opened after the
    latest such complaint, or is backing off after a failed open. In the caller's transaction."""
    now = clock.now()
    swept = Swept([], [])
    for account_id, _reported_at in complaint_candidates(session, now, since=since, limit=limit):
        try_open(
            session,
            clock,
            account_id,
            LINKED_COMPLAINTS,
            swept,
            backoff=backoff,
            kb_version_id=kb_version_id,
            p_mule_fallback=p_mule_fallback,
            demo_time=demo_time,
        )
    return swept


def mule_candidates(
    session: Session, now: datetime, *, threshold: float, window: timedelta, limit: int
) -> list[UUID]:
    """docs/09 §1: home-bank accounts whose stored mule score is at least ``threshold``, credited
    in ``(now − window, now]`` after their latest review (none open, none opened at or after the
    credit), oldest credit first, at most ``limit`` accounts. Each account counts once (its
    oldest such credit), so one busy mule cannot fill a batch."""
    s, a, p, k = bank.account_scores.c, bank.accounts.c, bank.payments.c, cases.c
    reviewed = exists().where(
        k.subject_account_id == p.payee_account_id,
        k.type == CaseType.ACCOUNT_REVIEW.value,
        or_(k.status != CaseStatus.DISPOSED.value, k.opened_at >= p.executed_at),
    )
    first_credit = (
        select(p.payee_account_id, p.executed_at, p.id)
        .select_from(
            bank.account_scores.join(bank.accounts, a.id == s.account_id).join(
                bank.payments, p.payee_account_id == s.account_id
            )
        )
        .where(
            s.p_mule >= threshold,
            a.bank_code == bank.HOME_BANK,
            p.status == EXECUTED,
            p.executed_at > now - window,
            p.executed_at <= now,
            ~reviewed,
            ~backing_off(p.payee_account_id, now),
        )
        .ext(distinct_on(p.payee_account_id))
        .order_by(p.payee_account_id, p.executed_at, p.id)
        .subquery()
    )
    f = first_credit.c
    return list(
        session.execute(
            select(f.payee_account_id).order_by(f.executed_at, f.id).limit(limit)
        ).scalars()
    )


def open_mule_reviews(
    session: Session,
    clock: Clock,
    *,
    kb_version_id: int,
    limit: int,
    backoff: Backoff,
    demo_time: bool = False,
) -> Swept:
    """docs/09 §1, the mule-scorer trigger: try up to ``limit`` suspects (above), with the
    mule-scorer prior alone (no complaint to add). In the caller's transaction."""
    version = get_version(session, kb_version_id)
    if version is None:
        raise DelphiError(f"No KB version {kb_version_id}.", code=ProblemCode.VALIDATION_FAILED)
    navigator = version.kb.config.navigator
    swept = Swept([], [])
    for account_id in mule_candidates(
        session,
        clock.now(),
        threshold=navigator.mule_sweep_threshold,
        window=timedelta(hours=navigator.mule_credit_window_h),
        limit=limit,
    ):
        try_open(
            session,
            clock,
            account_id,
            None,
            swept,
            backoff=backoff,
            kb_version_id=kb_version_id,
            demo_time=demo_time,
        )
    return swept


def begin_sweep_batch(session: Session) -> bool:
    """Bound this transaction and take the sweep lock; False if another run holds it."""
    session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL work_mem = '{WORK_MEM}'"))
    return bool(
        session.execute(
            select(func.pg_try_advisory_xact_lock(ACCOUNT_REVIEW_SWEEP_LOCK_KEY))
        ).scalar_one()
    )


@dataclass(frozen=True, slots=True)
class AccountReviewSweepJob:
    """``ACCOUNT_REVIEW_SWEEP`` (every minute, docs/04 §5.4): both docs/09 §1 triggers, the
    complaints first (a complained-about suspect gets the complaint's update). One transaction
    per batch; at most ``limits.max_per_run`` candidates tried per run, of both triggers
    together, opened or failed (a failed one backs off, #290)."""

    sessions: sessionmaker[Session]
    clock: Clock
    p_mule_fallback: float
    demo_time: bool
    limits: SweepLimits

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        counts = {"complaints": 0, "mule_scores": 0}
        failed = 0
        since = self.clock.now() - self.limits.complaint_lookback

        def tried() -> int:
            return sum(counts.values()) + failed

        def result(**extra: Any) -> dict[str, Any]:
            return {"opened": sum(counts.values()), **counts, "failed": failed, **extra}

        while tried() < self.limits.max_per_run:
            room = min(self.limits.batch, self.limits.max_per_run - tried())
            with self.sessions.begin() as session:
                if not begin_sweep_batch(session):
                    return result(skipped="busy")
                active = active_version(session)
                if active is None:
                    return result(reason="no ACTIVE KB version")
                complaints = open_complaint_reviews(
                    session,
                    self.clock,
                    kb_version_id=active.id,
                    p_mule_fallback=self.p_mule_fallback,
                    since=since,
                    limit=room,
                    backoff=self.limits.backoff,
                    demo_time=self.demo_time,
                )
                left = room - len(complaints.opened) - len(complaints.failed)
                mules = (
                    open_mule_reviews(
                        session,
                        self.clock,
                        kb_version_id=active.id,
                        limit=left,
                        backoff=self.limits.backoff,
                        demo_time=self.demo_time,
                    )
                    if left > 0
                    else Swept([], [])
                )
            counts["complaints"] += len(complaints.opened)
            counts["mule_scores"] += len(mules.opened)
            failed += len(complaints.failed) + len(mules.failed)
            if not (complaints.opened or complaints.failed or mules.opened or mules.failed):
                return result(more=False)
            if not ctx.report(tried() / self.limits.max_per_run):
                break
        return result(more=True)
