"""Platform endpoints: liveness and readiness (docs/16 §2, docs/20 §3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.kb.store import kb_readiness
from delphi.platform.health import CheckStatus, check_readiness
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["platform"])


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


class HealthView(BaseModel):
    status: Literal["ok"]
    version: str
    started_at: datetime


class CheckView(BaseModel):
    status: CheckStatus
    detail: str | None = None


class ReadinessView(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckView]


@router.get("/health")
def health(request: Request) -> HealthView:
    """Liveness and startup probe. Never touches the database, so a database outage does not
    cause restart loops."""
    p = _platform(request)
    return HealthView(status="ok", version=p.settings.version, started_at=p.started_at)


@router.get(
    "/ready",
    responses={503: {"model": ReadinessView, "description": "One or more checks failed"}},
)
def ready(request: Request, response: Response) -> ReadinessView:
    p = _platform(request)
    result = check_readiness(
        p.engine, p.migration_head, p.settings.ready_db_timeout_ms, extra={"kb": kb_readiness}
    )
    if not result.ready:
        response.status_code = 503
    return ReadinessView(
        status="ready" if result.ready else "not_ready",
        checks={
            name: CheckView(status=c.status, detail=c.detail) for name, c in result.checks.items()
        },
    )
