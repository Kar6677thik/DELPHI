"""The FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from delphi.api.problems import (
    PROBLEM_RESPONSES,
    document_problem_media_type,
    install_problem_handlers,
)
from delphi.api.request_context import RequestContextMiddleware
from delphi.api.routers import API_PREFIX, platform
from delphi.api.spa import install_spa
from delphi.platform.wiring import Platform


def create_app(resources: Platform) -> FastAPI:
    app = FastAPI(
        title="DELPHI",
        version=resources.settings.version,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
        responses=PROBLEM_RESPONSES,
    )
    app.state.platform = resources
    install_problem_handlers(app)
    document_problem_media_type(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(platform.router)
    # Last: the SPA catch-all must not shadow any API route.
    install_spa(app, resources.settings.static_dir)
    return app
