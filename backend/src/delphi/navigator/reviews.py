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
credited in the last 24 h, with the mule-scorer prior alone.

``ACCOUNT_REVIEW_SWEEP`` runs both every minute (docs/04 §5.4). Each is idempotent per account:
a case opens only for a complaint, or a credit, newer than the account's latest review, so a
disposed review is not reopened by what opened it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, func, select
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
from delphi.navigator.tables import cases
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobContext
from delphi.platform.logging import get_logger
from delphi.sim import api as bank

log = get_logger(__name__)

LINKED_COMPLAINTS = "LINKED_COMPLAINTS"
MULE = "MULE"
GENUINE = "GENUINE"
EXECUTED = "EXECUTED"
CREDITS_WINDOW = timedelta(days=7)  # docs/02: an account's amount at stake is its 7-day credits
MULE_CREDIT_WINDOW = timedelta(hours=24)  # docs/09 §1: "credits in the last 24 h"
NO_ANOMALY = 0.0  # docs/06 §11: no account anomaly model, so the open mass is the base


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


def complained_accounts(session: Session, now: datetime) -> dict[UUID, datetime]:
    """Home-bank accounts named as counterparty by a complaint known by ``now``, with the
    latest such complaint's time."""
    c = bank.complaints.c
    latest: dict[UUID, datetime] = {}
    for named, reported_at in session.execute(
        select(c.counterparty_account_ids, c.reported_at).where(c.reported_at <= now)
    ):
        for account_id in named:
            if account_id not in latest or reported_at > latest[account_id]:
                latest[account_id] = reported_at
    if not latest:
        return {}
    a = bank.accounts.c
    home = set(
        session.execute(
            select(a.id).where(a.id.in_(list(latest)), a.bank_code == bank.HOME_BANK)
        ).scalars()
    )
    return {k: v for k, v in latest.items() if k in home}


def latest_review_opened(session: Session) -> Mapping[UUID, datetime]:
    k = cases.c
    rows = session.execute(
        select(k.subject_account_id, func.max(k.opened_at))
        .where(k.type == CaseType.ACCOUNT_REVIEW.value)
        .group_by(k.subject_account_id)
    ).all()
    return {r[0]: r[1] for r in rows}


def open_complaint_reviews(
    session: Session,
    clock: Clock,
    *,
    kb_version_id: int,
    p_mule_fallback: float,
    demo_time: bool = False,
) -> list[CaseRecord]:
    """docs/09 §1: open a review for each home-bank account a complaint names, unless it has an
    open review, or a review opened after the latest complaint naming it."""
    now = clock.now()
    reviewed = latest_review_opened(session)
    opened: list[CaseRecord] = []
    for account_id, complained_at in sorted(complained_accounts(session, now).items()):
        last = reviewed.get(account_id)
        if last is not None and last >= complained_at:
            continue
        if open_review_for_account(session, account_id) is not None:
            continue
        p_mule = stored_p_mule(session, account_id)
        source = "account_scores"
        if p_mule is None:
            log.warning("navigator.p_mule_fallback", account_id=str(account_id))
            p_mule, source = p_mule_fallback, "fallback"
        case = open_account_review(
            session,
            clock,
            account_id=account_id,
            kb_version_id=kb_version_id,
            p_mule=p_mule,
            p_mule_source=source,
            demo_time=demo_time,
        )
        request_evidence(
            session, clock, case.id, LINKED_COMPLAINTS, requested_by=None, on_open=True
        )
        refreshed = get_case(session, case.id)
        assert refreshed is not None
        opened.append(refreshed)
    return opened


def mule_suspects(session: Session, now: datetime, threshold: float) -> dict[UUID, datetime]:
    """docs/09 §1: home-bank accounts whose stored mule score is at least ``threshold`` and
    that were credited in the last 24 h, with their latest such credit."""
    s, a, p = bank.account_scores.c, bank.accounts.c, bank.payments.c
    rows = session.execute(
        select(s.account_id, func.max(p.executed_at))
        .join(bank.accounts, a.id == s.account_id)
        .join(bank.payments, p.payee_account_id == s.account_id)
        .where(
            s.p_mule >= threshold,
            a.bank_code == bank.HOME_BANK,
            p.status == EXECUTED,
            p.executed_at > now - MULE_CREDIT_WINDOW,
            p.executed_at <= now,
        )
        .group_by(s.account_id)
    ).all()
    return {r[0]: r[1] for r in rows}


def open_mule_reviews(
    session: Session, clock: Clock, *, kb_version_id: int, demo_time: bool = False
) -> list[CaseRecord]:
    """docs/09 §1, the mule-scorer trigger: a review for each suspect (above), unless it has an
    open review, or a review opened after its latest credit (no new money since). The prior is
    the mule-scorer prior alone (no complaint to add)."""
    version = get_version(session, kb_version_id)
    if version is None:
        raise DelphiError(f"No KB version {kb_version_id}.", code=ProblemCode.VALIDATION_FAILED)
    now = clock.now()
    threshold = version.kb.config.navigator.mule_sweep_threshold
    reviewed = latest_review_opened(session)
    opened: list[CaseRecord] = []
    for account_id, credited_at in sorted(mule_suspects(session, now, threshold).items()):
        last = reviewed.get(account_id)
        if last is not None and last >= credited_at:
            continue
        if open_review_for_account(session, account_id) is not None:
            continue
        p_mule = stored_p_mule(session, account_id)
        assert p_mule is not None  # a suspect has a stored score
        opened.append(
            open_account_review(
                session,
                clock,
                account_id=account_id,
                kb_version_id=kb_version_id,
                p_mule=p_mule,
                p_mule_source="account_scores",
                demo_time=demo_time,
            )
        )
    return opened


@dataclass(frozen=True, slots=True)
class AccountReviewSweepJob:
    """``ACCOUNT_REVIEW_SWEEP`` (every minute, docs/04 §5.4): both docs/09 §1 triggers, the
    complaints first (a complained-about suspect gets the complaint's update)."""

    sessions: sessionmaker[Session]
    clock: Clock
    p_mule_fallback: float
    demo_time: bool

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        with self.sessions.begin() as session:
            active = active_version(session)
            if active is None:
                return {"opened": 0, "reason": "no ACTIVE KB version"}
            complaints = open_complaint_reviews(
                session,
                self.clock,
                kb_version_id=active.id,
                p_mule_fallback=self.p_mule_fallback,
                demo_time=self.demo_time,
            )
            mules = open_mule_reviews(
                session, self.clock, kb_version_id=active.id, demo_time=self.demo_time
            )
        return {
            "opened": len(complaints) + len(mules),
            "complaints": len(complaints),
            "mule_scores": len(mules),
        }
