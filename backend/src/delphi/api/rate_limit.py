"""The global API rate limit as ASGI middleware (docs/16 §1, #74).

Every request under ``/api/`` takes a token from its caller's bucket: 20 per second, bursts of
40 (``DELPHI_RATE_LIMIT_PER_S`` and ``DELPHI_RATE_LIMIT_BURST``). An empty bucket answers
``429 RATE_LIMITED`` with ``Retry-After``. The caller is its session (the cookie, hashed, so
the token is never kept in memory in the clear) or, without one, its address. The liveness and
readiness probes are exempt, and the SPA's static files are outside ``/api/``. The SSE stream is
one request, so it takes one token however long it stays open.
"""

from __future__ import annotations

import hashlib
import math
from http.cookies import CookieError, SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

from delphi.api.problems import problem_response
from delphi.api.routers import API_PREFIX
from delphi.api.security import SESSION_COOKIE
from delphi.platform.errors import ProblemCode
from delphi.platform.rate_limit import TokenBuckets

EXEMPT = frozenset({f"{API_PREFIX}/health", f"{API_PREFIX}/ready"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _session(scope: Scope) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    try:
        morsel = SimpleCookie(raw).get(SESSION_COOKIE)
    except CookieError:
        return None
    return morsel.value if morsel is not None and morsel.value else None


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, buckets: TokenBuckets, client_ip_header: str | None) -> None:
        self.app = app
        self.buckets = buckets
        self.client_ip_header = client_ip_header.lower().encode() if client_ip_header else None

    def _caller(self, scope: Scope) -> str:
        session = _session(scope)
        if session is not None:
            return "session:" + hashlib.sha256(session.encode()).hexdigest()
        if self.client_ip_header and (ip := _header(scope, self.client_ip_header)):
            return "ip:" + ip.strip()
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path in EXEMPT:
            await self.app(scope, receive, send)
            return
        wait = self.buckets.take(self._caller(scope))
        if wait is None:
            await self.app(scope, receive, send)
            return
        response = problem_response(
            ProblemCode.RATE_LIMITED,
            "Too many requests. Slow down and try again shortly.",
            instance=path,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
        await response(scope, receive, send)
