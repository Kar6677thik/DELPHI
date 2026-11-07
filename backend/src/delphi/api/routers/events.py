"""GET /api/v1/events/stream: Server-Sent Events (docs/16 §3, ADR-0013, FR-PLT-08).

The stream subscribes to the hub *before* replaying, so nothing committed during the replay
is lost; events already sent are skipped by id. A heartbeat comment (`: ping`) goes out every
15 s of silence, which also keeps proxies (Cloudflare) from closing the connection.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.events import TOPIC_PREFIXES, Event, EventHub, fetch_since, matches

router = APIRouter(prefix=API_PREFIX, tags=["events"])

HEARTBEAT_S = 15.0
HEARTBEAT_FRAME = ": ping\n\n"
ALL_TOPICS = frozenset(TOPIC_PREFIXES)


def _frame(event: Event) -> str:
    data = {
        "type": event.type,
        "entity_id": event.entity_id,
        "at": event.at.isoformat().replace("+00:00", "Z"),
        "payload": event.payload,
    }
    return (
        f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
    )


def _topics(raw: str | None) -> frozenset[str]:
    if raw is None or not raw.strip():
        return ALL_TOPICS
    topics = frozenset(t.strip() for t in raw.split(",") if t.strip())
    unknown = sorted(topics - ALL_TOPICS)
    if unknown:
        known = ", ".join(sorted(ALL_TOPICS))
        raise DelphiError(
            f"Unknown topics {unknown}; use {known}.", code=ProblemCode.VALIDATION_FAILED
        )
    return topics


def _last_event_id(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    if not raw.isdigit():
        raise DelphiError("Last-Event-ID must be an event id.", code=ProblemCode.VALIDATION_FAILED)
    return int(raw)


@router.get(
    "/events/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "An SSE stream"}},
)
async def stream(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    topics: Annotated[str | None, Query(description="Comma-separated topics")] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    hub: EventHub | None = request.app.state.event_hub
    if hub is None:
        raise DelphiError("Live updates are not running.", code=ProblemCode.NOT_INITIALISED)
    wanted = _topics(topics)
    resume_after = _last_event_id(last_event_id)
    sub = hub.subscribe(wanted)

    async def events() -> AsyncIterator[str]:
        sent = resume_after or 0
        try:
            if resume_after is not None:
                while batch := await asyncio.to_thread(fetch_since, hub.sessions, sent):
                    for event in batch:
                        sent = event.id
                        if matches(event.type, wanted):
                            yield _frame(event)
            while not sub.closed:
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield HEARTBEAT_FRAME
                    continue
                if item is None:  # the hub closed this subscription
                    return
                if item.id > sent:
                    sent = item.id
                    yield _frame(item)
        finally:
            hub.unsubscribe(sub)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
