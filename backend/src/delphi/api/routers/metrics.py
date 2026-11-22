"""The live KPIs and the Overview's alerts (docs/16 "Metrics", docs/22 §7, docs/20 §4, #126).

``GET /metrics/summary`` computes docs/22 §7's figures over the live demo data in the last
``hours`` (24 by default): what the bank knows, never ground truth. The open queue and the
alerts are as of now.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.guardian.api import guardian_kpis
from delphi.navigator.api import case_kpis
from delphi.platform.alerts import NOT_TRACKED, active_alerts
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["metrics"])
DEFAULT_HOURS = 24
MAX_HOURS = 24 * 30


class GuardianSummary(BaseModel):
    decided: int
    decisions: dict[str, int]
    median_session_seconds: float | None
    active_probes_per_session: float | None
    info_yield_bits: dict[str, float]
    held: int
    label_coverage_held: float | None
    dd_pack_completeness: float | None


class CaseSummary(BaseModel):
    open_cases: int
    open_by_protocol: dict[str, int]
    sla_breaches: int
    disposed: int
    median_hold_resolution_minutes: float | None


class AlertView(BaseModel):
    code: str
    message: str
    count: int


class MetricsSummary(BaseModel):
    window_hours: int
    as_of: str
    guardian: GuardianSummary
    cases: CaseSummary
    # docs/22 §7: the last chase's golden-hour ratio; null until the Chase loop exists (M6).
    golden_hour_ratio: float | None
    alerts: list[AlertView]
    # docs/20 §4 alerts with no data source yet, so a quiet banner is not read as "all clear".
    alerts_not_tracked: list[str]


@router.get("/metrics/summary")
def read_summary(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    hours: Annotated[int, Query(ge=1, le=MAX_HOURS)] = DEFAULT_HOURS,
) -> MetricsSummary:
    platform: Platform = request.app.state.platform
    now = platform.clock.now()
    since = now - timedelta(hours=hours)
    with platform.sessions() as session:
        g = guardian_kpis(session, since=since, until=now)
        c = case_kpis(session, now=now, since=since, until=now)
        alerts = active_alerts(session, now=now)
    return MetricsSummary(
        window_hours=hours,
        as_of=now.isoformat(),
        guardian=GuardianSummary(
            decided=g.decided,
            decisions=dict(g.decisions),
            median_session_seconds=g.median_session_seconds,
            active_probes_per_session=g.active_probes_per_session,
            info_yield_bits=dict(g.info_yield_bits),
            held=g.held,
            label_coverage_held=g.label_coverage_held,
            dd_pack_completeness=g.dd_pack_completeness,
        ),
        cases=CaseSummary(
            open_cases=c.open_cases,
            open_by_protocol=dict(c.open_by_protocol),
            sla_breaches=c.sla_breaches,
            disposed=c.disposed,
            median_hold_resolution_minutes=c.median_hold_resolution_minutes,
        ),
        golden_hour_ratio=None,
        alerts=[AlertView(code=a.code, message=a.message, count=a.count) for a in alerts],
        alerts_not_tracked=list(NOT_TRACKED),
    )
