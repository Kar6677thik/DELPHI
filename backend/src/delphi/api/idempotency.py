"""Idempotency-Key for routers (docs/16 §1). Usage, in a state-creating route::

    @router.post("/things", status_code=201)
    def create(request: Request, body: RawBody, key: IdempotencyKey, principal: ...):
        return run_idempotent(request, body, principal, key,
                              lambda session: (201, make_thing(session)))

``run_idempotent`` opens the request's transaction, replays a stored response or runs the
handler, and stores its response in that same transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from delphi.api.security import Principal
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.idempotency import lookup, request_fingerprint, store
from delphi.platform.wiring import Platform

REPLAY_HEADER = "Idempotent-Replayed"


def _key(idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> UUID:
    if idempotency_key is None:
        raise DelphiError(
            "This request needs an Idempotency-Key header.", code=ProblemCode.VALIDATION_FAILED
        )
    try:
        return UUID(idempotency_key)
    except ValueError:
        raise DelphiError(
            "Idempotency-Key must be a UUID.", code=ProblemCode.VALIDATION_FAILED
        ) from None


async def _body(request: Request) -> bytes:
    return await request.body()


IdempotencyKey = Annotated[UUID, Depends(_key)]
# The raw request body, for the fingerprint (sync routes cannot await it themselves).
RawBody = Annotated[bytes, Depends(_body)]


def run_idempotent(
    request: Request,
    body: bytes,
    principal: Principal,
    key: UUID,
    handler: Callable[[Session], tuple[int, Any]],
) -> JSONResponse:
    platform: Platform = request.app.state.platform
    endpoint = f"{request.method} {request.url.path}"
    fingerprint = request_fingerprint(endpoint, body)
    now = platform.clock.now()
    with platform.sessions.begin() as session:
        stored = lookup(
            session,
            user_id=principal.user_id,
            key=key,
            endpoint=endpoint,
            fingerprint=fingerprint,
            now=now,
            retention=platform.settings.idempotency_retention,
        )
        if stored is not None:
            return JSONResponse(
                stored.body, status_code=stored.status, headers={REPLAY_HEADER: "true"}
            )
        status, result = handler(session)
        payload = jsonable_encoder(result)
        store(
            session,
            user_id=principal.user_id,
            key=key,
            endpoint=endpoint,
            fingerprint=fingerprint,
            status=status,
            body=payload,
            now=now,
        )
    return JSONResponse(payload, status_code=status)
