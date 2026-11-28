"""The Overview's alert banner (docs/20 §4, #126): conditions an operator should act on.

Computed on request from the jobs and the LLM call log. Three of docs/20 §4's alerts have no
source yet and are reported as not tracked rather than as quiet: the backup age (needs
`ops_backup_runs`, M9), the SSE outbox lag (needs the publisher's position) and
`PROBE_YIELD_DEGRADED` (#121).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from delphi.platform.tables import jobs, llm_calls

# docs/20 §4's thresholds: they define the alerts, so they live with them.
FAILURE_WINDOW = timedelta(hours=1)
QUEUED_TOO_LONG = timedelta(minutes=10)
LLM_RECENT_CALLS = 20
LLM_FALLBACK_SHARE = 0.5
TEMPLATE = "template"  # the provider name of TemplateLlm's rows in llm_calls
NOT_TRACKED = ("BACKUP_AGE", "SSE_OUTBOX_LAG", "PROBE_YIELD_DEGRADED")


@dataclass(frozen=True, slots=True)
class Alert:
    code: str
    message: str
    count: int


def active_alerts(session: Session, *, now: datetime) -> list[Alert]:
    out: list[Alert] = []
    j = jobs.c
    failed = session.execute(
        select(func.count()).where(j.status == "FAILED", j.finished_at >= now - FAILURE_WINDOW)
    ).scalar_one()
    if failed:
        out.append(Alert("JOB_FAILURES", f"{failed} job(s) failed in the last hour.", failed))
    stuck = session.execute(
        select(func.count()).where(j.status == "QUEUED", j.run_after < now - QUEUED_TOO_LONG)
    ).scalar_one()
    if stuck:
        out.append(
            Alert("JOBS_QUEUED", f"{stuck} job(s) have waited over 10 minutes to start.", stuck)
        )
    # The last 20 calls: a fallback is a template row written after the model was tried, so
    # the share is the template rows among calls made while a model provider was in use.
    recent = (
        select(llm_calls.c.provider)
        .order_by(llm_calls.c.created_at.desc())
        .limit(LLM_RECENT_CALLS)
        .subquery()
    )
    total, templated = session.execute(
        select(func.count(), func.count().filter(recent.c.provider == TEMPLATE))
    ).one()
    model_calls = int(total) - int(templated)
    if model_calls and int(templated) / int(total) > LLM_FALLBACK_SHARE:
        out.append(
            Alert(
                "LLM_FALLBACK",
                f"{templated} of the last {total} narratives fell back to the template.",
                int(templated),
            )
        )
    return out
