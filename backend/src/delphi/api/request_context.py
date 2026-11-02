"""Request id and the structured request log line (docs/16 §1, docs/20 §1).

A pure ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``, which buffers and
breaks streaming responses (SSE, docs/16 §3).
"""

from __future__ import annotations

import re
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from delphi.api.problems import internal_error_response
from delphi.api.request_id import request_id_var
from delphi.platform.ids import uuid7
from delphi.platform.logging import bind_context, clear_context, get_logger

REQUEST_ID_HEADER = "x-request-id"
# A client-supplied id is echoed only if it is short and harmless in logs and headers.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MS_PER_S = 1000

log = get_logger("delphi.http")


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode():
            candidate: str = value.decode("latin-1")
            if _SAFE_REQUEST_ID.match(candidate):
                return candidate
    return str(uuid7())


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        token = request_id_var.set(request_id)
        clear_context()
        bind_context(request_id=request_id)
        started = time.perf_counter()
        status: dict[str, Any] = {"code": 500, "started": False}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                status["started"] = True
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            log.error("http.unhandled_exception", exc_info=exc)
            if status["started"]:
                raise  # too late for an error response; the server closes the connection
            await internal_error_response(scope["path"])(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            log.info(
                "http.request",
                method=scope["method"],
                # The route template, never the raw path: bounded cardinality, no ids (docs/20 §2).
                route=getattr(route, "path", "<unmatched>"),
                status=status["code"],
                duration_ms=round((time.perf_counter() - started) * _MS_PER_S, 2),
            )
            clear_context()
            request_id_var.reset(token)
