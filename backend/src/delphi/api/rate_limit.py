"""The global API rate limit as ASGI middleware (docs/16 §1, #74).

Every request under ``/api/`` takes a token from its caller's bucket: 20 per second, bursts of
40 (``DELPHI_RATE_LIMIT_PER_S`` and ``DELPHI_RATE_LIMIT_BURST``). An empty bucket answers
``429 RATE_LIMITED`` with ``Retry-After``. The caller is its session (the cookie, hashed, so
the token is never kept in memory in the clear) or, without one, its address.

Two more buckets are keyed by the client address alone, because the cookie is the caller's to
choose (#127): any cookie value got a fresh bucket, so rotating cookies escaped the limit.
- every request also takes a token from its address's bucket (``DELPHI_RATE_LIMIT_ADDRESS_*``,
  higher than a session's, so people sharing an address are not throttled);
- ``POST /auth/login`` takes one from its address's sign-in bucket (``DELPHI_LOGIN_RATE_*``),
  since each attempt costs an Argon2 hash, whatever the username.

The liveness and
readiness probes are exempt, and the SPA's static files are outside ``/api/``. The SSE stream is
one request, so it takes one token however long it stays open.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

from delphi.api.problems import problem_response
from delphi.api.routers import API_PREFIX
from delphi.api.security import SESSION_COOKIE
from delphi.platform.errors import ProblemCode
from delphi.platform.rate_limit import TokenBuckets

EXEMPT = frozenset({f"{API_PREFIX}/health", f"{API_PREFIX}/ready"})
LOGIN = f"{API_PREFIX}/auth/login"


@dataclass(frozen=True, slots=True)
class RateLimits:
    callers: TokenBuckets  # per session, or per address without one
    addresses: TokenBuckets  # per address, every request
    logins: TokenBuckets  # per address, sign-in attempts


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
    def __init__(self, app: ASGIApp, limits: RateLimits, client_ip_header: str | None) -> None:
        self.app = app
        self.limits = limits
        self.client_ip_header = client_ip_header.lower().encode() if client_ip_header else None

    def _address(self, scope: Scope) -> str:
        if self.client_ip_header and (ip := _header(scope, self.client_ip_header)):
            return "ip:" + ip.strip()
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    def _wait(self, scope: Scope, path: str) -> float | None:
        """None if every bucket the request draws on has a token, else the longest wait. The
        buckets are tried in order, and the first refusal stops: a refused request costs no
        tokens from the buckets after it."""
        address = self._address(scope)
        session = _session(scope)
        caller = (
            "session:" + hashlib.sha256(session.encode()).hexdigest()
            if session is not None
            else address
        )
        draws = [(self.limits.addresses, address), (self.limits.callers, caller)]
        if path == LOGIN and scope.get("method") == "POST":
            draws.insert(0, (self.limits.logins, address))
        for buckets, key in draws:
            wait = buckets.take(key)
            if wait is not None:
                return wait
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path in EXEMPT:
            await self.app(scope, receive, send)
            return
        wait = self._wait(scope, path)
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
