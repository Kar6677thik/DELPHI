"""Campaign recompute, around the cases that changed (docs/05 §5, docs/09 §8, #107).

``CAMPAIGN_RECOMPUTE`` applies the merge rule (``delphi.navigator.campaigns``) only where it can
have changed, in bounded batches (the #263 pattern; the owner's design on #259):
- **Touched cases.** The cases opened, disposed or given new evidence since the job's watermark
  (``campaign_watermark``), oldest first, ``limits.batch`` per transaction. Every touch time is
  Clock time (``opened_at``, ``disposed_at``, ``evidence_records.requested_at``), taken before
  its transaction commits, so only touches at least ``limits.settle_lag`` old are taken: a
  transaction that commits late (within the lag) is still behind the watermark's next move. The
  watermark moves with each committed batch, so a crash loses at most one batch.
- **Entities.** A case's infrastructure entities are written to ``case_entities`` the first time
  the job sees it:
  - a PAYMENT_HOLD case: the payee account, the payment's device fingerprint, and the /24 prefix
    of the payer's latest login at or before the payment;
  - an ACCOUNT_REVIEW case: the account under review.
- **The component.** From the touched cases, the job follows shared entities to cases with the
  same leading hypothesis opened within the window (indexed lookups on ``case_entities``), adds
  the other members of the campaigns it meets. Only that component is regrouped. A component
  that reaches ``limits.max_component`` cases is **cut**: the job writes no campaign or case
  change for it (a partial regroup would split or dissolve campaigns wrongly), moves the
  watermark past the batch, and logs and counts it.
- **Saved.**
  - New campaigns are inserted, and the loaded cases whose ``campaign_id`` changed are set or
    cleared in one ``UPDATE … FROM (VALUES …)``.
  - A merged campaign keeps its row with ``merged_into`` and ``case_count = 0``; a dissolved
    one keeps its row with ``case_count = 0``.
  - Each campaign's aggregates follow docs/05 §5. ``expected_loss_paise`` is Σ risk(RELEASE)
    over its cases. ``vulnerable_victims`` counts its distinct vulnerable payers. ``rank_score``
    is expected loss × (1 + 0.5 · vulnerable share), the share being over its distinct payers.
  - ``campaign.updated`` is published for every campaign that changed.
- **Gap fill.** ``f3_stage_max`` stays null: the spec names the F3 stages (A-07) but not how a
  case maps to them (asked on #117).

A case's ``version`` is not bumped when its campaign changes: the campaign is derived, and an
investigator's If-Match on the case should not fail because a recompute ran.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    and_,
    column,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    union_all,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.store import active_version
from delphi.navigator.campaigns import NEVER_IN_A_CAMPAIGN, CaseFacts, assign_ids, groups
from delphi.navigator.tables import (
    campaign_watermark,
    campaigns,
    case_entities,
    cases,
    evidence_records,
)
from delphi.platform.clock import Clock
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobContext
from delphi.platform.locks import CAMPAIGN_RECOMPUTE_LOCK_KEY
from delphi.platform.logging import get_logger
from delphi.platform.settings import Settings
from delphi.sim import api as bank

log = get_logger(__name__)

RELEASE = "RELEASE"
VULNERABLE_RANK_WEIGHT = 0.5  # docs/05 §5: rank_score = loss × (1 + 0.5 · vulnerable share)
# Limits for the recompute transaction (docs/23: postgres runs in 1 GiB), as LABEL_DELIVERY's.
STATEMENT_TIMEOUT = "30s"
WORK_MEM = "16MB"


@dataclass(frozen=True, slots=True)
class LoadedCase:
    facts: CaseFacts
    release_risk_paise: float  # risk(RELEASE) under the case posterior; 0 if not stored
    payer_customer_id: UUID | None  # the victim of a payment case
    payer_vulnerable: bool


def _facts_query() -> Any:
    k = cases.c
    p, a, cu, d, li = (
        bank.payments.c,
        bank.accounts.c,
        bank.customers.c,
        bank.devices.c,
        bank.login_events.c,
    )
    login = (
        select(li.ip_prefix24)
        .where(li.customer_id == a.customer_id, li.at <= p.initiated_at)
        .order_by(li.at.desc())
        .limit(1)
        .correlate(bank.accounts, bank.payments)
        .scalar_subquery()
    )
    return select(
        k.id,
        k.opened_at,
        k.leading_hypothesis,
        k.campaign_id,
        k.subject_account_id,
        k.risk_by_decision,
        p.payee_account_id,
        d.fingerprint,
        a.customer_id.label("payer_customer_id"),
        cu.is_vulnerable,
        func.host(login).label("login_ip"),
    ).select_from(
        cases.outerjoin(bank.payments, p.id == k.subject_payment_id)
        .outerjoin(bank.accounts, a.id == p.payer_account_id)
        .outerjoin(bank.customers, cu.id == a.customer_id)
        .outerjoin(bank.devices, d.id == p.device_id)
    )


def _loaded(row: Any) -> LoadedCase:
    entities: set[str] = set()
    if row.payee_account_id is not None:
        entities.add(f"account:{row.payee_account_id}")
    if row.subject_account_id is not None:
        entities.add(f"account:{row.subject_account_id}")
    if row.fingerprint is not None:
        entities.add(f"device:{row.fingerprint}")
    if row.login_ip is not None:
        entities.add(f"ip24:{row.login_ip}/24")
    risk = (row.risk_by_decision or {}).get(RELEASE, 0.0)
    return LoadedCase(
        CaseFacts(
            row.id, row.opened_at, row.leading_hypothesis, frozenset(entities), row.campaign_id
        ),
        float(risk),
        row.payer_customer_id,
        bool(row.is_vulnerable),
    )


def load_cases(session: Session, ids: Iterable[UUID]) -> list[LoadedCase]:
    k = cases.c
    query = _facts_query().where(k.id.in_(list(ids)))
    return [_loaded(r) for r in session.execute(query.order_by(k.opened_at, k.id))]


def touched(
    session: Session, after: tuple[datetime, UUID], *, until: datetime, limit: int
) -> list[tuple[datetime, UUID]]:
    """The next ``limit`` (time, case) touches after ``after`` and at or before ``until``: a
    case opened, disposed, or given an evidence record (all Clock times)."""
    k, e = cases.c, evidence_records.c
    at, last = after
    src = union_all(
        select(k.opened_at.label("at"), k.id.label("case_id")).where(
            k.opened_at >= at, k.opened_at <= until
        ),
        select(k.disposed_at, k.id).where(k.disposed_at >= at, k.disposed_at <= until),
        select(e.requested_at, e.case_id).where(e.requested_at >= at, e.requested_at <= until),
    ).subquery("touched")
    rows = session.execute(
        select(src.c.at, src.c.case_id)
        .where(tuple_(src.c.at, src.c.case_id) > tuple_(literal(at), literal(last)))
        .order_by(src.c.at, src.c.case_id)
        .limit(limit)
    )
    return [(r.at, r.case_id) for r in rows]


def record_entities(session: Session, loaded: Sequence[LoadedCase]) -> None:
    """Store each case's entities once (they do not change: the payment and the login are
    history)."""
    values = [
        {"case_id": m.facts.case_id, "entity": entity}
        for m in loaded
        for entity in sorted(m.facts.entities)
    ]
    if values:
        session.execute(pg_insert(case_entities).values(values).on_conflict_do_nothing())


def component(
    session: Session, seeds: set[UUID], *, window: timedelta, limit: int
) -> tuple[set[UUID], bool]:
    """The cases linked to ``seeds`` (shared entity, same leading hypothesis, opened within
    ``window``), closed over, plus the other members of every campaign met; at most ``limit``
    cases. Returns them and whether the limit cut the search short."""
    a, b = case_entities.alias("a"), case_entities.alias("b")
    k1, k2, k = cases.alias("k1"), cases.alias("k2"), cases.c
    members, frontier = set(seeds), set(seeds)
    while frontier:
        room = limit - len(members)
        if room <= 0:
            return members, True
        linked = (
            select(b.c.case_id)
            .distinct()
            .select_from(
                a.join(k1, k1.c.id == a.c.case_id)
                .join(b, b.c.entity == a.c.entity)
                .join(k2, k2.c.id == b.c.case_id)
            )
            .where(
                a.c.case_id.in_(list(frontier)),
                k1.c.leading_hypothesis.not_in(NEVER_IN_A_CAMPAIGN),
                k2.c.leading_hypothesis == k1.c.leading_hypothesis,
                k2.c.opened_at.between(k1.c.opened_at - window, k1.c.opened_at + window),
                b.c.case_id.not_in(list(members)),
            )
            .limit(room + 1)
        )
        found = set(session.execute(linked).scalars())
        in_campaigns = select(k.campaign_id).where(
            k.id.in_(list(frontier | found)), k.campaign_id.is_not(None)
        )
        found |= set(
            session.execute(
                select(k.id)
                .where(k.campaign_id.in_(in_campaigns), k.id.not_in(list(members | found)))
                .limit(room + 1)
            ).scalars()
        )
        if len(found) > room:
            members |= set(sorted(found, key=str)[:room])
            return members, True
        members |= found
        frontier = found
    return members, False


@dataclass(frozen=True, slots=True)
class Aggregates:
    leading_hypothesis: str
    case_count: int
    expected_loss_paise: int
    vulnerable_victims: int
    rank_score: float


def aggregates(members: Sequence[LoadedCase]) -> Aggregates:
    """docs/05 §5's campaign fields over its cases."""
    loss = round(sum(max(m.release_risk_paise, 0.0) for m in members))
    victims = {m.payer_customer_id: m.payer_vulnerable for m in members if m.payer_customer_id}
    vulnerable = sum(victims.values())
    share = vulnerable / len(victims) if victims else 0.0
    return Aggregates(
        members[0].facts.leading_hypothesis,
        len(members),
        loss,
        vulnerable,
        loss * (1 + VULNERABLE_RANK_WEIGHT * share),
    )


def begin_recompute(session: Session) -> bool:
    """Bound this transaction and take the recompute lock; False if another run holds it."""
    session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL work_mem = '{WORK_MEM}'"))
    return bool(
        session.execute(
            select(func.pg_try_advisory_xact_lock(CAMPAIGN_RECOMPUTE_LOCK_KEY))
        ).scalar_one()
    )


def _created_at(session: Session, ids: set[UUID]) -> dict[UUID, datetime]:
    if not ids:
        return {}
    c = campaigns.c
    return dict(session.execute(select(c.id, c.created_at).where(c.id.in_(ids))).tuples().all())


def regroup(
    session: Session, clock: Clock, loaded: Sequence[LoadedCase], *, window_days: float
) -> dict[str, int]:
    """docs/05 §5: group ``loaded`` (a closed component) and save the campaigns, in the caller's
    transaction (which holds the recompute lock). Returns counts of what changed."""
    now = clock.now()
    facts = [m.facts for m in loaded]
    members = groups(facts, window_days=window_days)
    previous = {f.campaign_id for f in facts if f.campaign_id is not None}
    assignment = assign_ids(
        facts,
        members,
        created_at=_created_at(session, previous),
        new_id=lambda: uuid7(unix_ms=int(now.timestamp() * 1000)),
    )
    by_case = {m.facts.case_id: m for m in loaded}
    k, c = cases.c, campaigns.c
    changed: set[UUID] = set()
    for campaign_id, group in assignment.new_campaigns.items():
        agg = aggregates([by_case[i] for i in group])
        session.execute(
            campaigns.insert().values(
                id=campaign_id,
                leading_hypothesis=agg.leading_hypothesis,
                case_count=agg.case_count,
                f3_stage_max=None,
                expected_loss_paise=agg.expected_loss_paise,
                vulnerable_victims=agg.vulnerable_victims,
                rank_score=agg.rank_score,
                merged_into=None,
                created_at=now,
                updated_at=now,
            )
        )
        changed.add(campaign_id)
    moves = [
        (f.case_id, assignment.campaign_of.get(f.case_id))
        for f in facts
        if assignment.campaign_of.get(f.case_id) != f.campaign_id
    ]
    if moves:
        target = values_list(
            column("case_id", PG_UUID(as_uuid=True)),
            column("campaign_id", PG_UUID(as_uuid=True)),
            name="moves",
        ).data(moves)
        session.execute(
            update(cases).where(k.id == target.c.case_id).values(campaign_id=target.c.campaign_id)
        )
    moved = len(moves)
    for old, survivor in assignment.merged_into.items():
        session.execute(
            update(campaigns)
            .where(c.id == old)
            .values(merged_into=survivor, case_count=0, updated_at=now)
        )
        changed.add(old)
    for gone in assignment.dissolved:
        session.execute(update(campaigns).where(c.id == gone).values(case_count=0, updated_at=now))
        changed.add(gone)
    survivors = {assignment.campaign_of[g[0]] for g in members} - set(assignment.new_campaigns)
    for campaign_id in survivors:
        in_campaign = [
            by_case[i] for i, cid in assignment.campaign_of.items() if cid == campaign_id
        ]
        agg = aggregates(in_campaign)
        stored = session.execute(select(campaigns).where(c.id == campaign_id)).one()
        values = {
            "leading_hypothesis": agg.leading_hypothesis,
            "case_count": agg.case_count,
            "expected_loss_paise": agg.expected_loss_paise,
            "vulnerable_victims": agg.vulnerable_victims,
            "rank_score": agg.rank_score,
            "merged_into": None,
        }
        if any(getattr(stored, key) != value for key, value in values.items()):
            session.execute(
                update(campaigns).where(c.id == campaign_id).values(**values, updated_at=now)
            )
            changed.add(campaign_id)
    for campaign_id in sorted(changed, key=str):
        publish(
            session, EventType.CAMPAIGN_UPDATED, str(campaign_id), {"campaign_id": str(campaign_id)}
        )
    return {
        "cases": len(facts),
        "campaigns": len(members),
        "new": len(assignment.new_campaigns),
        "merged": len(assignment.merged_into),
        "moved_cases": moved,
        "changed": len(changed),
    }


@dataclass(frozen=True, slots=True)
class Limits:
    batch: int  # touched cases per transaction
    batches_per_run: int
    max_component: int  # the most cases one regroup loads
    settle_lag: timedelta  # longer than the longest transaction that touches a case

    @staticmethod
    def of(settings: Settings) -> Limits:
        return Limits(
            settings.campaign_batch,
            settings.campaign_batches_per_run,
            settings.campaign_max_component,
            timedelta(seconds=settings.campaign_settle_lag_s),
        )


def run_batch(
    session: Session, clock: Clock, *, window_days: float, limits: Limits
) -> dict[str, int] | None:
    """One batch: the next settled touches, their component, regrouped; the watermark moves past
    them. None when nothing was touched. A cut component writes no campaign or case change.
    In the caller's transaction (it holds the lock)."""
    w = campaign_watermark.c
    mark = session.execute(select(w.at, w.case_id).where(w.id == 1)).one()
    until = clock.now() - limits.settle_lag
    batch = touched(session, (mark.at, mark.case_id), until=until, limit=limits.batch)
    if not batch:
        return None
    seeds = {case_id for _, case_id in batch}
    record_entities(session, load_cases(session, seeds))
    members, cut = component(
        session, seeds, window=timedelta(days=window_days), limit=limits.max_component
    )
    if cut:
        log.warning("campaigns.component_cut", seeds=len(seeds), limit=limits.max_component)
        counts = dict.fromkeys(("cases", "campaigns", "new", "merged", "moved_cases", "changed"), 0)
    else:
        loaded = load_cases(session, members)
        record_entities(session, loaded)
        counts = regroup(session, clock, loaded, window_days=window_days)
    last_at, last_case = batch[-1]
    session.execute(
        update(campaign_watermark).where(w.id == 1).values(at=last_at, case_id=last_case)
    )
    return counts | {"touched": len(batch), "component_cut": int(cut)}


@dataclass(frozen=True, slots=True)
class CampaignRecomputeJob:
    """``CAMPAIGN_RECOMPUTE`` (every 60 s, docs/04 §5.4): one transaction per batch of touched
    cases, at most ``limits.batches_per_run`` per run, under the ACTIVE KB's
    ``navigator.campaign_window_days``; skips while another run recomputes."""

    sessions: sessionmaker[Session]
    clock: Clock
    limits: Limits

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        totals: dict[str, int] = {
            "batches": 0,
            "touched": 0,
            "new": 0,
            "merged": 0,
            "changed": 0,
            "component_cut": 0,
        }
        for n in range(self.limits.batches_per_run):
            with self.sessions.begin() as session:
                if not begin_recompute(session):
                    return {**totals, "skipped": "busy"}
                version = active_version(session)
                if version is None:
                    return {**totals, "skipped": "no ACTIVE KB version"}
                counts = run_batch(
                    session,
                    self.clock,
                    window_days=version.kb.config.navigator.campaign_window_days,
                    limits=self.limits,
                )
            if counts is None:
                return {**totals, "more": False}
            totals["batches"] += 1
            for key in ("touched", "new", "merged", "changed", "component_cut"):
                totals[key] += counts[key]
            if not ctx.report((n + 1) / self.limits.batches_per_run):
                break
        return {**totals, "more": True}


def list_campaigns(
    session: Session, *, limit: int, after: tuple[float, UUID] | None = None
) -> list[Any]:
    """The live campaigns (not merged, with cases), highest rank first (keyset by
    ``(rank_score, id)``)."""
    c = campaigns.c
    query = select(campaigns).where(c.merged_into.is_(None), c.case_count > 0)
    if after is not None:
        rank, last = after
        query = query.where(or_(c.rank_score < rank, and_(c.rank_score == rank, c.id < last)))
    return list(session.execute(query.order_by(c.rank_score.desc(), c.id.desc()).limit(limit)))


def get_campaign(session: Session, campaign_id: UUID) -> Any | None:
    return session.execute(select(campaigns).where(campaigns.c.id == campaign_id)).one_or_none()


MEMBER_CASES_LIMIT = 200  # docs/16: the rest are at GET /cases?campaign_id=
SHARED_ENTITIES_LIMIT = 20


def member_cases(
    session: Session, campaign_id: UUID, *, limit: int = MEMBER_CASES_LIMIT
) -> list[Any]:
    """The campaign's oldest ``limit`` cases, with what the campaign page shows of each."""
    k = cases.c
    return list(
        session.execute(
            select(
                k.id, k.type, k.status, k.leading_hypothesis, k.opened_at, k.amount_at_stake_paise
            )
            .where(k.campaign_id == campaign_id)
            .order_by(k.opened_at, k.id)
            .limit(limit)
        )
    )


def shared_entities(
    session: Session, campaign_id: UUID, *, limit: int = SHARED_ENTITIES_LIMIT
) -> list[tuple[str, int]]:
    """The entities at least two of the campaign's cases have (docs/09 §8), most shared first,
    at most ``limit``: ``(entity, case count)``."""
    e, k = case_entities.c, cases.c
    n = func.count().label("n")
    rows = session.execute(
        select(e.entity, n)
        .join(cases, k.id == e.case_id)
        .where(k.campaign_id == campaign_id)
        .group_by(e.entity)
        .having(func.count() > 1)
        .order_by(n.desc(), e.entity)
        .limit(limit)
    )
    return [(r.entity, int(r.n)) for r in rows]
