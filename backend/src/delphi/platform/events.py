"""Live events: the transactional outbox and the api's fan-out hub (docs/04 §5.5, ADR-0013).

**Publishing** (any process): ``publish`` inserts an `outbox_events` row and issues
`NOTIFY delphi_events` in the caller's transaction. If the transaction rolls back, the event
never existed, and NOTIFY is only delivered on commit.

**Ordering.** Clients resume with `Last-Event-ID`, which only works if event ids become visible
in increasing order. An identity value is taken at insert time, but transactions commit in any
order, so a reader could see id 11 before id 10 commits and skip 10 for good. ``publish``
therefore takes a transaction-scoped advisory lock first: publishing transactions commit in id
order. Publish as the last step of a transaction, so the lock is held briefly.

**Fan-out** (api process): ``EventHub.run`` starts a listener thread holding one connection
that LISTENs. On each notification, and every ``poll_s`` as a safety net, it reads the rows
after the last id it has seen and hands them to the event loop, which gives each event to every
subscriber whose topics match. A subscriber whose queue is
full is closed; its client reconnects with `Last-Event-ID` and replays from the outbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.locks import OUTBOX_LOCK_KEY
from delphi.platform.logging import get_logger
from delphi.platform.tables import outbox_events

log = get_logger(__name__)

CHANNEL = "delphi_events"
# The most rows one read takes from the outbox; the hub reads again until it has caught up.
FETCH_BATCH = 500


class EventType(StrEnum):
    """docs/16 §3."""

    GUARDIAN_SESSION_UPDATED = "guardian.session.updated"
    GUARDIAN_SESSION_DECIDED = "guardian.session.decided"
    CASE_OPENED = "case.opened"
    CASE_UPDATED = "case.updated"
    EVIDENCE_NARRATIVE_READY = "evidence.narrative_ready"
    CAMPAIGN_UPDATED = "campaign.updated"
    CHASE_UPDATED = "chase.updated"
    CHASE_LIEN_UPDATED = "chase.lien.updated"
    JOB_UPDATED = "job.updated"
    SCOUT_CLUSTER_CREATED = "scout.cluster.created"
    KB_VERSION_CHANGED = "kb.version.changed"
    ARTIFACT_ACTIVATED = "artifact.activated"
    SIM_TRANSFER = "sim.transfer"


# The `topics=` names of GET /events/stream and the event-type prefixes each one selects.
TOPIC_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "guardian": ("guardian.",),
    "cases": ("case.", "evidence.", "campaign."),
    "chase": ("chase.",),
    "jobs": ("job.",),
    "scout": ("scout.",),
    "kb": ("kb.",),
    "sensing": ("artifact.",),
    "sim": ("sim.",),
}


def matches(event_type: str, topics: frozenset[str]) -> bool:
    return any(event_type.startswith(p) for t in topics for p in TOPIC_PREFIXES[t])


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    type: str
    entity_id: str | None
    payload: Mapping[str, Any]
    at: datetime


def publish(
    session: Session,
    event_type: EventType,
    entity_id: str | None,
    payload: Mapping[str, Any] | None = None,
) -> int:
    """Add an event in the caller's transaction and return its id. Payloads stay small:
    clients re-fetch the entity over REST (docs/16 §3)."""
    session.execute(select(func.pg_advisory_xact_lock(OUTBOX_LOCK_KEY)))
    event_id: int = session.execute(
        outbox_events.insert()
        .values(type=event_type.value, entity_id=entity_id, payload=dict(payload or {}))
        .returning(outbox_events.c.id)
    ).scalar_one()
    session.execute(select(func.pg_notify(CHANNEL, str(event_id))))
    return event_id


def fetch_since(
    sessions: sessionmaker[Session], after_id: int, limit: int = FETCH_BATCH
) -> list[Event]:
    """Outbox events with id > ``after_id``, oldest first."""
    with sessions() as session:
        rows = session.execute(
            select(outbox_events)
            .where(outbox_events.c.id > after_id)
            .order_by(outbox_events.c.id)
            .limit(limit)
        ).all()
    return [Event(r.id, r.type, r.entity_id, r.payload, r.at) for r in rows]


def latest_id(sessions: sessionmaker[Session]) -> int:
    with sessions() as session:
        value: int | None = session.execute(select(func.max(outbox_events.c.id))).scalar_one()
    return value or 0


@dataclass(eq=False, slots=True)
class Subscription:
    topics: frozenset[str]
    queue: asyncio.Queue[Event | None]
    closed: bool = False


@dataclass(slots=True)
class EventHub:
    """One per api process. ``listen_dsn`` is a libpq URL (``postgresql://…``)."""

    sessions: sessionmaker[Session]
    listen_dsn: str
    queue_size: int = 1000
    poll_s: float = 5.0
    reconnect_s: float = 2.0
    # Set while a LISTEN connection is up (health checks and tests wait on it).
    listening: threading.Event = field(default_factory=threading.Event)
    _subscribers: set[Subscription] = field(default_factory=set)

    def subscribe(self, topics: Iterable[str]) -> Subscription:
        sub = Subscription(frozenset(topics), asyncio.Queue(maxsize=self.queue_size))
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def deliver(self, events: Iterable[Event]) -> None:
        """Hand events to matching subscribers. Runs on the event loop."""
        for event in events:
            for sub in list(self._subscribers):
                if not matches(event.type, sub.topics):
                    continue
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    # A slow client: close it rather than block everyone. It reconnects with
                    # Last-Event-ID and replays what it missed from the outbox.
                    log.warning("sse.subscriber_overflow")
                    self._close(sub)

    def _close(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)
        sub.closed = True
        with contextlib.suppress(asyncio.QueueFull):
            sub.queue.put_nowait(None)

    def _listen(self, loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
        """The listener thread: a plain (blocking) connection that LISTENs, so this works on
        every platform's event loop. Each batch read from the outbox is handed to the loop."""
        last_id = latest_id(self.sessions)

        def catch_up() -> None:
            nonlocal last_id
            while events := fetch_since(self.sessions, last_id):
                last_id = events[-1].id
                loop.call_soon_threadsafe(self.deliver, events)
                if len(events) < FETCH_BATCH:
                    return

        while not stop.is_set():
            try:
                with psycopg.connect(self.listen_dsn, autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    catch_up()  # events committed while nobody was listening
                    self.listening.set()
                    log.info("sse.listening")
                    while not stop.is_set():
                        # Wake on a notification, or after poll_s as a safety net.
                        for _ in conn.notifies(timeout=self.poll_s, stop_after=1):
                            pass
                        catch_up()
            except Exception as exc:
                self.listening.clear()
                log.warning("sse.listener_error", error=type(exc).__name__)
                stop.wait(self.reconnect_s)
        self.listening.clear()

    async def run(self) -> None:
        """Fan out until cancelled (the api's lifespan task)."""
        stop = threading.Event()
        thread = threading.Thread(
            target=self._listen,
            args=(asyncio.get_running_loop(), stop),
            name="sse-listener",
            daemon=True,
        )
        thread.start()
        try:
            await asyncio.Event().wait()  # until cancelled
        finally:
            stop.set()
            for sub in list(self._subscribers):
                self._close(sub)
            await asyncio.to_thread(thread.join, self.poll_s + self.reconnect_s + 1)
