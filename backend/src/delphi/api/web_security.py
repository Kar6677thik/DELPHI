"""CSRF checks and security headers (docs/19 §4, docs/16 §1).

**CSRF.** The session cookie is `SameSite=Strict`. On top of that, every mutating request to
the api (anything but GET, HEAD and OPTIONS under `/api/`) must carry `X-Delphi-Csrf: 1` and an
`Origin` equal to the public origin; otherwise `403 CSRF_REJECTED`. A cross-site form cannot set
a custom header, and a cross-site script cannot fake `Origin`. Login is covered too, which stops
login CSRF. When no public origin is configured (local development), the request's own origin
(scheme and host) is used.

**Headers.** Every response, including the SPA and errors, gets the docs/19 §4 headers.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from delphi.api.problems import problem_response
from delphi.platform.errors import ProblemCode

CSRF_HEADER = b"x-delphi-csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        b"connect-src 'self'; frame-ancestors 'none'",
    ),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _own_origin(scope: Scope) -> str | None:
    host = _header(scope, b"host")
    return f"{scope.get('scheme', 'http')}://{host}" if host else None


class WebSecurityMiddleware:
    def __init__(self, app: ASGIApp, public_origin: str | None) -> None:
        self.app = app
        self.public_origin = public_origin.rstrip("/") if public_origin else None

    def _csrf_ok(self, scope: Scope) -> bool:
        if scope["method"] in SAFE_METHODS or not scope["path"].startswith("/api/"):
            return True
        if _header(scope, CSRF_HEADER) != "1":
            return False
        expected = self.public_origin or _own_origin(scope)
        return expected is not None and _header(scope, b"origin") == expected

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    h
                    for h in message.get("headers", [])
                    if h[0] not in {name for name, _ in SECURITY_HEADERS}
                ]
                message = {**message, "headers": [*headers, *SECURITY_HEADERS]}
            await send(message)

        if not self._csrf_ok(scope):
            response = problem_response(
                ProblemCode.CSRF_REJECTED,
                "Mutating requests need the X-Delphi-Csrf header and a same-site Origin.",
                instance=scope["path"],
            )
            await response(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)
