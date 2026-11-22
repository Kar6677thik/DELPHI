"""The Navigator's live KPIs for the Overview (docs/22 §7, docs/18 §2 "queues summary", #126):
the open case queue by protocol, SLA breaches, and how long holds took to resolve."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from delphi.navigator.store import CaseStatus
from delphi.navigator.tables import cases

_SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True, slots=True)
class CaseKpis:
    open_cases: int
    open_by_protocol: Mapping[str, int]
    sla_breaches: int  # open cases past their SLA now
    disposed: int  # in [since, until)
    median_hold_resolution_minutes: float | None


def case_kpis(session: Session, *, now: datetime, since: datetime, until: datetime) -> CaseKpis:
    c = cases.c
    is_open = c.status != CaseStatus.DISPOSED.value
    by_protocol = {
        str(p): int(n)
        for p, n in session.execute(
            select(c.protocol, func.count()).where(is_open).group_by(c.protocol)
        )
    }
    breaches = session.execute(select(func.count()).where(is_open, c.sla_due_at < now)).scalar_one()
    in_window = and_(
        c.status == CaseStatus.DISPOSED.value, c.disposed_at >= since, c.disposed_at < until
    )
    disposed, median = session.execute(
        select(
            func.count(),
            func.percentile_cont(0.5).within_group(
                func.extract("epoch", c.disposed_at - c.opened_at)
            ),
        ).where(in_window)
    ).one()
    return CaseKpis(
        open_cases=sum(by_protocol.values()),
        open_by_protocol=dict(sorted(by_protocol.items())),
        sla_breaches=int(breaches),
        disposed=int(disposed),
        median_hold_resolution_minutes=(
            None if median is None else float(median) / _SECONDS_PER_MINUTE
        ),
    )
