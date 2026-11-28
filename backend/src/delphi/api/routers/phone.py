"""The mock phone (docs/16 "Guardian", docs/08 §4, docs/18 §2 `/phone`, #98).

`GET /phone/personas` lists the demo's customers with their accounts, saved payees and devices.
`GET /phone/stream?customer_id=` is the customer's channel: Server-Sent Events `probe.requested`,
`otp.sms`, `probe.attempt_result` and `session.decided`, each `data` the docs/08 §4 object with
its `type`. They come from the api's runner slot, not the outbox, so they carry no `id:` and
cannot be resumed with `Last-Event-ID`; instead a probe still waiting for an answer is sent
again to every new connection (an OTP with its SMS). `: open` goes out first and `: ping` every
15 s of silence, as on `/events/stream` (docs/16 §3).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.routers.events import HEARTBEAT_FRAME, HEARTBEAT_S, OPEN_FRAME
from delphi.api.security import Permission, Principal, require
from delphi.guardian.api import LiveGuardian, PhoneEvent
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.wiring import Platform
from delphi.sim.api import customer_exists, phone_personas

router = APIRouter(prefix=API_PREFIX, tags=["phone"])


class PersonaAccountView(BaseModel):
    id: UUID
    label: str
    vpa: str | None
    balance_paise: int


class PersonaPayeeView(BaseModel):
    account_id: UUID
    label: str
    vpa: str | None
    added_at: datetime


class PersonaDeviceView(BaseModel):
    id: UUID
    label: str
    os: str
    first_seen_at: datetime


class PersonaView(BaseModel):
    """A demo customer (docs/14 §6) as the phone's picker shows them."""

    customer_id: UUID
    persona_key: str
    name: str
    language: str
    vulnerable: bool
    accounts: list[PersonaAccountView]
    payees: list[PersonaPayeeView]
    devices: list[PersonaDeviceView]


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


@router.get("/phone/personas")
def read_personas(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.DEMO_DRIVE))],
) -> list[PersonaView]:
    """The persona customers (docs/14 §6), by persona key; empty until a dataset with personas
    is seeded. Accounts are the customer's own at this bank; devices newest first."""
    with _platform(request).sessions() as session:
        found = phone_personas(session)
    return [
        PersonaView(
            customer_id=p.customer_id,
            persona_key=p.persona_key,
            name=p.name,
            language=p.language,
            vulnerable=p.vulnerable,
            accounts=[PersonaAccountView(**asdict(a)) for a in p.accounts],
            payees=[PersonaPayeeView(**asdict(y)) for y in p.payees],
            devices=[PersonaDeviceView(**asdict(d)) for d in p.devices],
        )
        for p in found
    ]


def _frame(event: PhoneEvent) -> str:
    return f"event: {event.type}\ndata: {json.dumps(dict(event.data), separators=(',', ':'))}\n\n"


@router.get(
    "/phone/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "An SSE stream"}},
)
async def stream(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.DEMO_DRIVE))],
    customer_id: Annotated[UUID, Query()],
) -> StreamingResponse:
    """The customer's phone channel. 404 for an unknown customer; 503 NOT_INITIALISED while the
    Guardian does not run in this process."""
    live: LiveGuardian | None = request.app.state.guardian_runner
    if live is None:
        raise DelphiError(
            "The Guardian is not running in this process yet.", code=ProblemCode.NOT_INITIALISED
        )

    def known() -> bool:
        with _platform(request).sessions() as session:
            return customer_exists(session, customer_id)

    if not await asyncio.to_thread(known):
        raise NotFoundError("No customer with this id.")
    sub = live.hub.subscribe(customer_id)

    async def events() -> AsyncIterator[str]:
        try:
            yield OPEN_FRAME
            while not sub.closed:
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield HEARTBEAT_FRAME
                    continue
                if item is None:  # the hub closed this connection: the phone reconnects
                    return
                yield _frame(item)
        finally:
            live.hub.unsubscribe(sub)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
