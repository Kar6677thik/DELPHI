"""Dataset and model readiness (docs/16 §2 "Platform", docs/20 §3, #88).

``GET /status`` tells the UI whether the demo is initialised, and how far the seeding has got.
It is not readiness: ``/ready`` never depends on the dataset or the models, so the first deploy
becomes healthy before seeding finishes (docs/23 §4).

``Initialised`` is the dependency feature endpoints declare (Guardian, Navigator, Chase, …): it
answers ``503 NOT_INITIALISED`` until the dataset and every model are ready.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.wiring import Platform
from delphi.sensing.api import ArtifactKind, initialisation

router = APIRouter(prefix=API_PREFIX, tags=["platform"])


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


class ModelStatus(BaseModel):
    version: int
    gate_passed: bool


class StatusView(BaseModel):
    dataset_ready: bool
    models_ready: bool
    seed_job_id: UUID | None
    seed_progress: float | None
    # The ACTIVE model of each kind, and those activated below their gate (docs/13 §4): the
    # Overview page shows "sensing below quality gate" while this is not empty.
    models: dict[ArtifactKind, ModelStatus]
    models_below_gate: list[ArtifactKind]


@router.get("/status")
def read_status(
    request: Request, _principal: Annotated[Principal, Depends(require(Permission.VIEW))]
) -> StatusView:
    with _platform(request).sessions() as session:
        state = initialisation(session)
    job = state.seed_job
    return StatusView(
        dataset_ready=state.dataset_ready,
        models_ready=state.models_ready,
        seed_job_id=job.id if job is not None else None,
        seed_progress=job.progress if job is not None else None,
        models={
            k: ModelStatus(version=a.version, gate_passed=a.gate_passed)
            for k, a in sorted(state.active.items())
        },
        models_below_gate=sorted(state.below_gate),
    )


def require_initialised(request: Request) -> None:
    """503 ``NOT_INITIALISED`` until the dataset and every sensing model are ready."""
    with _platform(request).sessions() as session:
        state = initialisation(session)
    if not state.ready:
        missing = [] if state.dataset_ready else ["dataset"]
        missing += [k.value for k in ArtifactKind if k not in state.active]
        raise DelphiError(
            f"DELPHI is still initialising (waiting for: {', '.join(missing)}).",
            code=ProblemCode.NOT_INITIALISED,
        )


Initialised = Annotated[None, Depends(require_initialised)]
