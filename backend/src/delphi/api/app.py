"""The FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from delphi.api.problems import install_problem_handlers
from delphi.api.request_context import RequestContextMiddleware
from delphi.api.routers import API_PREFIX, platform
from delphi.platform.wiring import Platform


def create_app(resources: Platform) -> FastAPI:
    app = FastAPI(
        title="DELPHI",
        version=resources.settings.version,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.platform = resources
    install_problem_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(platform.router)
    return app
