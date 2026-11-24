"""Demo control (docs/16 §2 "Admin and jobs", docs/14 §10, #101): run a demo story.

`POST /admin/demo/scenarios/{story}/run` queues a `DEMO_STORY` job (202 with the job): the
worker holds the persona manifest (`sim_truth`), which the api never reads. In `autoplay` the
worker plays the story on its own Guardian runner; in `interactive` the job's result is the
payment the presenter's mock phone pays with `POST /guardian/sessions`, so the session runs on
this process's runner and the phone answers it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.routers.jobs import JobView
from delphi.api.routers.status import require_initialised
from delphi.api.security import Permission, Principal, require
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.jobs import get_job
from delphi.platform.wiring import Platform
from delphi.sim.api import RUNNABLE_STORIES, StoryMode, StoryName, request_story

router = APIRouter(prefix=API_PREFIX, tags=["admin"])
# Declared first, so a caller without the permission gets 403 before any other check.
Driver = Annotated[Principal, Depends(require(Permission.DEMO_DRIVE))]


class StoryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StoryMode


def _ready(request: Request, story: StoryName, mode: StoryMode) -> None:
    """422 for a story that does not run yet; 503 NOT_INITIALISED until the story can run."""
    if story not in RUNNABLE_STORIES:
        raise DelphiError(
            f"Story {story} cannot be run yet: only {', '.join(RUNNABLE_STORIES)} run so far.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    require_initialised(request)
    platform: Platform = request.app.state.platform
    if not platform.settings.simulation_enabled:
        raise DelphiError(
            "The simulation is off (DELPHI_SIMULATION_ENABLED): no worker plays the stories.",
            code=ProblemCode.NOT_INITIALISED,
        )
    if mode == "interactive" and request.app.state.guardian_runner is None:
        raise DelphiError(
            "The Guardian is not running in this process yet.", code=ProblemCode.NOT_INITIALISED
        )


@router.post("/admin/demo/scenarios/{story}/run", status_code=202, response_model=JobView)
def run_story(
    *,
    driver: Driver,
    story: StoryName,
    body: StoryRunRequest,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """Queue a `DEMO_STORY` job for S1 or S2 (202 with the job). Its result names the story's
    customer and, in `autoplay`, the payment and its WORKER session; in `interactive`, the
    `payment` to pay from the mock phone. 422 for S3 and S4 (a later release) or an unknown
    mode; 503 NOT_INITIALISED until the dataset and models are ready, while the simulation is
    off, or (interactive) while this process has no Guardian runner."""
    _ready(request, story, body.mode)
    platform: Platform = request.app.state.platform

    def start(session: Session) -> tuple[int, Any]:
        queued = request_story(
            session,
            platform.clock,
            story=story,
            mode=body.mode,
            idempotency_key=f"DEMO_STORY:api:{key}",
            created_by=driver.user_id,
        )
        record = get_job(session, queued.job_id)
        assert record is not None
        return 202, JobView.of(record)

    return run_idempotent(request, raw, driver, key, start)
