"""Sensing administration (docs/16 §2 "Admin and jobs", docs/13 §7, #88): retrain the models."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.routers.jobs import JobView
from delphi.api.security import Permission, Principal, require
from delphi.platform.jobs import get_job
from delphi.platform.wiring import Platform
from delphi.sensing.api import request_training

router = APIRouter(prefix=API_PREFIX, tags=["admin"])
# Declared first, so a caller without the permission gets 403 before any other check.
Admin = Annotated[Principal, Depends(require(Permission.ADMIN))]


@router.post("/admin/sensing/train", status_code=202, response_model=JobView)
def train_sensing(
    admin: Admin, raw: RawBody, key: IdempotencyKey, request: Request
) -> JSONResponse:
    """Queue a `TRAIN_SENSING` job on the dataset in the bank (202 with the job). 409
    `INVALID_STATE_TRANSITION` without a dataset, 409 `JOB_ALREADY_RUNNING` while one runs. A
    model whose gate fails is stored as FAILED_GATE and the ACTIVE one is kept (docs/13 §4)."""
    platform: Platform = request.app.state.platform

    def start(session: Session) -> tuple[int, Any]:
        queued = request_training(
            session,
            platform.clock,
            idempotency_key=f"TRAIN_SENSING:api:{key}",
            created_by=admin.user_id,
        )
        record = get_job(session, queued.job_id)
        assert record is not None
        return 202, JobView.of(record)

    return run_idempotent(request, raw, admin, key, start)
