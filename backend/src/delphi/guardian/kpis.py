"""The Guardian's live KPIs for the Overview (docs/22 §7, FR-EVL-05, #126).

The same definitions as the evaluation (docs/22 §4), computed over live demo data, where there
is no ground truth: only what the bank itself knows (decisions, probe runs, labels). Every
figure is over the sessions decided in ``[since, until)``; a figure with nothing to count is
``None`` (shown as a dash), never 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.platform.labels import labels

# docs/12 §4: the decisions whose label coverage is reported, and the window a label counts in.
HELD_DECISIONS = ("HOLD_FOR_REVIEW", "PROTECTIVE_HOLD", "DECLINE")
LABEL_WINDOW = timedelta(days=7)
ACTIVE = "ACTIVE"
COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class GuardianKpis:
    decided: int
    decisions: Mapping[str, int]  # decision → count
    median_session_seconds: float | None
    active_probes_per_session: float | None
    info_yield_bits: Mapping[str, float]  # probe key → mean bits over completed runs
    held: int
    label_coverage_held: float | None  # docs/12 §4, a label known within 7 days
    dd_pack_completeness: float | None


def guardian_kpis(session: Session, *, since: datetime, until: datetime) -> GuardianKpis:
    s = guardian_sessions.c
    decided = and_(s.decided_at.is_not(None), s.decided_at >= since, s.decided_at < until)

    by_decision = {
        str(d): int(n)
        for d, n in session.execute(
            select(s.decision, func.count()).where(decided).group_by(s.decision)
        )
        if d is not None
    }
    total = sum(by_decision.values())

    median = session.execute(
        select(
            func.percentile_cont(0.5).within_group(
                func.extract("epoch", s.decided_at - s.started_at)
            )
        ).where(decided)
    ).scalar_one_or_none()

    p = probe_runs.c
    active = session.execute(
        select(func.count())
        .select_from(probe_runs.join(guardian_sessions, p.session_id == s.id))
        .where(decided, p.mode == ACTIVE)
    ).scalar_one()

    yields = {
        str(k): float(v)
        for k, v in session.execute(
            select(p.probe_key, func.avg(p.info_yield_bits))
            .select_from(probe_runs.join(guardian_sessions, p.session_id == s.id))
            .where(decided, p.status == COMPLETED, p.info_yield_bits.is_not(None))
            .group_by(p.probe_key)
            .order_by(p.probe_key)
        )
    }

    held_rows = and_(decided, s.decision.in_(HELD_DECISIONS))
    labelled = exists().where(
        labels.c.subject_type == "PAYMENT",
        labels.c.subject_id == s.payment_id,
        labels.c.known_at <= s.decided_at + LABEL_WINDOW,
    )
    held, covered = session.execute(
        select(func.count(), func.count().filter(labelled)).where(held_rows)
    ).one()

    complete = session.execute(
        select(func.count()).where(decided, s.dd_pack_complete.is_(True))
    ).scalar_one()

    return GuardianKpis(
        decided=total,
        decisions=dict(sorted(by_decision.items())),
        median_session_seconds=None if median is None else float(median),
        active_probes_per_session=active / total if total else None,
        info_yield_bits=yields,
        held=int(held),
        label_coverage_held=int(covered) / int(held) if held else None,
        dd_pack_completeness=complete / total if total else None,
    )
