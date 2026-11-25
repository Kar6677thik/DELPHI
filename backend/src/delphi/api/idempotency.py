"""Idempotency-Key for routers (docs/16 §1). Usage, in a state-creating route::

    @router.post("/things", status_code=201)
    def create(request: Request, body: RawBody, key: IdempotencyKey, principal: ...):
        return run_idempotent(request, body, principal, key,
                              lambda session: (201, make_thing(session)))

``run_idempotent`` opens the request's transaction, replays a stored response or runs the
handler, and stores its response in that same transaction.

``run_idempotent_async`` is for work that commits in its own steps (a Guardian session's,
docs/08 §2), which cannot share the key's transaction. Concurrent requests with one key wait on
an in-process lock for (user, key) while the work runs, so they still run it once; the api runs
one replica (docs/23 §4), like the rate limiter. The key is looked up, and the response stored,
in short transactions of their own, so no request holds a pooled connection while its work takes
more: holding one used to deadlock the pool under load (#286). A crash between the work and the
store frees the key with the work done (docs/16 §1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from delphi.api.security import Principal
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.idempotency import StoredResponse, lookup, request_fingerprint, store
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


class KeyLocks:
    """One asyncio lock per (user, key) in this process, dropped once no request holds or
    waits on it, so memory follows the requests in flight."""

    def __init__(self) -> None:
        self._locks: dict[tuple[UUID, UUID], tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, user_id: UUID, key: UUID) -> AsyncIterator[None]:
        name = (user_id, key)
        lock, users = self._locks.get(name, (asyncio.Lock(), 0))
        self._locks[name] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[name]
            if users == 1:
                del self._locks[name]
            else:
                self._locks[name] = (lock, users - 1)

    def __len__(self) -> int:
        return len(self._locks)


KEY_LOCKS = KeyLocks()


async def run_idempotent_async(
    request: Request,
    body: bytes,
    principal: Principal,
    key: UUID,
    handler: Callable[[], Awaitable[tuple[int, Any]]],
) -> JSONResponse:
    """``run_idempotent`` for a handler that commits its own work. The key's lock is held in
    process while the handler runs; its lookup and its store are short transactions, run off
    the event loop, so no pooled connection is held across the handler."""
    platform: Platform = request.app.state.platform
    endpoint = f"{request.method} {request.url.path}"
    fingerprint = request_fingerprint(endpoint, body)
    now = platform.clock.now()

    def found(session: Session) -> StoredResponse | None:
        return lookup(
            session,
            user_id=principal.user_id,
            key=key,
            endpoint=endpoint,
            fingerprint=fingerprint,
            now=now,
            retention=platform.settings.idempotency_retention,
        )

    def stored() -> StoredResponse | None:
        with platform.sessions.begin() as session:  # commits an expired key's delete
            return found(session)

    async with KEY_LOCKS.hold(principal.user_id, key):
        replay = await asyncio.to_thread(stored)
        if replay is not None:
            return JSONResponse(
                replay.body, status_code=replay.status, headers={REPLAY_HEADER: "true"}
            )
        status, result = await handler()
        payload = jsonable_encoder(result)

        def keep() -> None:
            with platform.sessions.begin() as session:
                # Another api process (a rolling update's second pod) may have stored it
                # meanwhile: the first response stays the key's.
                if found(session) is not None:
                    return
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

        await asyncio.to_thread(keep)
    return JSONResponse(payload, status_code=status)
