"""Datasets (docs/16 §2 "Admin and jobs", #19): ask the worker to generate one."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.routers.jobs import JobView
from delphi.api.security import Permission, Principal, require
from delphi.platform.jobs import get_job
from delphi.platform.wiring import Platform
from delphi.sim.api import DatasetProfile, request_dataset

router = APIRouter(prefix=API_PREFIX, tags=["admin"])
# Declared first, so a caller without the permission gets 403 before any other check.
Admin = Annotated[Principal, Depends(require(Permission.ADMIN))]
MAX_SEED = 2**63 - 1  # delphi.datasets.seed is a bigint


class DatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: DatasetProfile
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED, description="default: the profile's")


@router.post("/admin/datasets", status_code=202, response_model=JobView)
def create_dataset(
    admin: Admin,
    body: DatasetRequest,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """Queue a `GENERATE_DATASET` job (202 with the job). 409 `JOB_ALREADY_RUNNING` while one
    is under way, 409 `INVALID_STATE_TRANSITION` once the bank holds a dataset."""
    platform: Platform = request.app.state.platform

    def start(session: Session) -> tuple[int, Any]:
        params: dict[str, Any] = {"profile": body.profile}
        if body.seed is not None:
            params["seed"] = body.seed
        queued = request_dataset(
            session,
            platform.clock,
            idempotency_key=f"GENERATE_DATASET:api:{key}",
            params=params,
            created_by=admin.user_id,
            allowed_profiles=platform.settings.allowed_dataset_profiles,
        )
        record = get_job(session, queued.job_id)
        assert record is not None
        return 202, JobView.of(record)

    return run_idempotent(request, raw, admin, key, start)
