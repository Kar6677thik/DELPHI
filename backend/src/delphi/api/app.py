"""The FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from delphi.api.problems import (
    PROBLEM_RESPONSES,
    document_problem_media_type,
    install_problem_handlers,
)
from delphi.api.request_context import RequestContextMiddleware
from delphi.api.routers import API_PREFIX, events, jobs, platform
from delphi.api.spa import install_spa
from delphi.platform.events import EventHub
from delphi.platform.wiring import Platform


def create_app(resources: Platform, *, event_hub: EventHub | None = None) -> FastAPI:
    """``event_hub`` is the api process's live-event fan-out (docs/04 §5.5). Without one (tests,
    contract generation) GET /events/stream answers 503 NOT_INITIALISED."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(event_hub.run()) if event_hub is not None else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        lifespan=lifespan,
        title="DELPHI",
        version=resources.settings.version,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
        responses=PROBLEM_RESPONSES,
    )
    app.state.platform = resources
    app.state.event_hub = event_hub
    install_problem_handlers(app)
    document_problem_media_type(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(platform.router)
    app.include_router(events.router)
    app.include_router(jobs.router)
    # Last: the SPA catch-all must not shadow any API route.
    install_spa(app, resources.settings.static_dir)
    return app
