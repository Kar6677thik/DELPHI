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
from delphi.api.rate_limit import RateLimitMiddleware, RateLimits
from delphi.api.request_context import RequestContextMiddleware
from delphi.api.routers import (
    API_PREFIX,
    admin,
    auth,
    cases,
    chase,
    datasets,
    demo,
    events,
    guardian,
    jobs,
    kb,
    metrics,
    phone,
    platform,
    question_bank,
    scout,
    sensing,
    status,
)
from delphi.api.spa import install_spa
from delphi.api.web_security import WebSecurityMiddleware
from delphi.guardian.api import LiveGuardian
from delphi.platform.auth_sessions import LoginThrottle
from delphi.platform.events import EventHub
from delphi.platform.logging import get_logger
from delphi.platform.rate_limit import TokenBuckets
from delphi.platform.wiring import Platform

log = get_logger(__name__)


def create_app(
    resources: Platform,
    *,
    event_hub: EventHub | None = None,
    guardian_runner: LiveGuardian | None = None,
) -> FastAPI:
    """``event_hub`` is the api process's live-event fan-out (docs/04 §5.5). Without one (tests,
    contract generation) GET /events/stream answers 503 NOT_INITIALISED.

    ``guardian_runner`` is the runner slot (#98): the LIVE sessions of the mock phone, driven
    in this process. Without it the Guardian's write endpoints and the phone stream answer 503
    NOT_INITIALISED. Tests put one in with a fixture prior; ``delphi api`` fills it at start-up
    (#95, ``cli.live_guardian_for``), and the lifespan resumes its interrupted sessions first.
    The slot can also be set later, on ``app.state.guardian_runner``."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(event_hub.run()) if event_hub is not None else None
        live_at_start: LiveGuardian | None = _app.state.guardian_runner
        if live_at_start is not None:  # FR-GDN-14: finish the sessions a restart interrupted
            resumed = await live_at_start.runner.resume_all()
            log.info("guardian.resumed", sessions=len(resumed))
        try:
            yield
        finally:
            live: LiveGuardian | None = _app.state.guardian_runner
            if live is not None:  # its sessions resume on the next start-up (FR-GDN-14)
                await live.runner.shutdown()
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
    app.state.guardian_runner = guardian_runner
    s = resources.settings
    app.state.login_throttle = LoginThrottle(
        resources.clock, max_failures=s.login_max_failures, window=s.login_window
    )
    install_problem_handlers(app)
    document_problem_media_type(app)
    # Starlette runs the last-added middleware first: the request id wraps everything, so even
    # a CSRF rejection or a 429 carries one, and every response gets the security headers.
    app.state.rate_limit = RateLimits(
        callers=TokenBuckets(resources.clock, rate=s.rate_limit_per_s, burst=s.rate_limit_burst),
        addresses=TokenBuckets(
            resources.clock, rate=s.rate_limit_address_per_s, burst=s.rate_limit_address_burst
        ),
        logins=TokenBuckets(resources.clock, rate=s.login_rate_per_s, burst=s.login_rate_burst),
    )
    app.add_middleware(
        RateLimitMiddleware, limits=app.state.rate_limit, client_ip_header=s.client_ip_header
    )
    app.add_middleware(WebSecurityMiddleware, public_origin=s.public_origin)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(platform.router)
    app.include_router(status.router)
    app.include_router(auth.router)
    app.include_router(events.router)
    app.include_router(jobs.router)
    app.include_router(admin.router)
    app.include_router(datasets.router)
    app.include_router(demo.router)
    app.include_router(kb.router)
    app.include_router(question_bank.router)
    app.include_router(sensing.router)
    app.include_router(cases.router)
    app.include_router(chase.router)
    app.include_router(scout.router)
    app.include_router(guardian.router)
    app.include_router(phone.router)
    app.include_router(metrics.router)
    # Last: the SPA catch-all must not shadow any API route.
    install_spa(app, resources.settings.static_dir)
    return app
