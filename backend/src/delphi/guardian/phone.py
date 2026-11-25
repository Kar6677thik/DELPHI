"""The mock phone's customer channel (docs/08 §1 entry point 1, §4; docs/16 "Guardian", #98).

``PhoneHub`` fans the phone's events out to the open ``GET /phone/stream`` connections of one
customer, in the api process. They are never written to the outbox: an ``otp.sms`` carries the
live code, which DELPHI stores only as an HMAC (docs/08 §4). So that a phone opened (or
reconnected) while a probe waits still shows it, the hub keeps each session's waiting probe
(its ``probe.requested``, and the ``otp.sms`` of an OTP) until it is answered or the session
ends, and replays it to a new connection.

``PhoneChannel`` is the ``CustomerChannel`` of LIVE sessions: ``ask`` sends ``probe.requested``
(and the ``otp.sms``), ``finished`` sends ``session.decided``.

``LiveGuardian`` is what the api holds in its runner slot (``app.state.guardian``): the service,
the session runner, the ``AnswerBoard`` and the hub, with the three phone operations of
docs/16 (start, answer, cancel). Answers go through ``GuardianService.respond`` and then the
board, as docs/08 §2 "Runner" requires. The real slot is filled at api start-up (#95).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from delphi.guardian.active import ClientSignals
from delphi.guardian.runner import AnswerBoard, SessionRunner
from delphi.guardian.service import GuardianService, IssuedProbe, Responded
from delphi.guardian.store import (
    GuardianSessionRecord,
    ProbeRunStatus,
    Runner,
    SessionMode,
)
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.logging import get_logger
from delphi.sim import api as bank

log = get_logger(__name__)

PROBE_REQUESTED = "probe.requested"
OTP_SMS = "otp.sms"
PROBE_ATTEMPT_RESULT = "probe.attempt_result"
SESSION_DECIDED = "session.decided"
PHONE_EVENT_TYPES = (PROBE_REQUESTED, OTP_SMS, PROBE_ATTEMPT_RESULT, SESSION_DECIDED)
# Events one phone connection may have unread before it is closed: a phone holds a handful.
PHONE_QUEUE_SIZE = 100


@dataclass(frozen=True, slots=True)
class PhoneEvent:
    """One SSE event to the phone: ``data`` is the docs/08 §4 object, ``type`` included."""

    type: str
    data: Mapping[str, Any]


@dataclass(eq=False)
class PhoneSubscription:
    customer_id: UUID
    queue: asyncio.Queue[PhoneEvent | None]
    closed: bool = False


@dataclass
class PhoneHub:
    """In-process fan-out by customer. Every method runs on the event loop. A subscriber whose
    queue is full is closed (its phone reconnects and gets the waiting probe again)."""

    queue_size: int = PHONE_QUEUE_SIZE
    _subs: dict[UUID, set[PhoneSubscription]] = field(default_factory=dict)
    # customer → session → the waiting probe's events, replayed to a new connection
    _waiting: dict[UUID, dict[UUID, list[PhoneEvent]]] = field(default_factory=dict)

    def subscribe(self, customer_id: UUID) -> PhoneSubscription:
        sub = PhoneSubscription(customer_id, asyncio.Queue(maxsize=self.queue_size))
        self._subs.setdefault(customer_id, set()).add(sub)
        for events in self._waiting.get(customer_id, {}).values():
            for event in events:
                self._put(sub, event)
        return sub

    def unsubscribe(self, sub: PhoneSubscription) -> None:
        subs = self._subs.get(sub.customer_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._subs[sub.customer_id]

    def publish(
        self, customer_id: UUID, session_id: UUID, event: PhoneEvent, *, waiting: bool = False
    ) -> None:
        """Send ``event`` to the customer's phones. ``waiting`` keeps it for later connections
        while the probe waits; a ``probe.requested`` replaces whatever the session kept."""
        if event.type == PROBE_REQUESTED:
            self.settle(customer_id, session_id)
        if waiting:
            self._waiting.setdefault(customer_id, {}).setdefault(session_id, []).append(event)
        for sub in list(self._subs.get(customer_id, ())):
            self._put(sub, event)

    def forget_waiting(self) -> None:
        """Drop every waiting probe kept for new connections (the demo reset deleted them)."""
        self._waiting.clear()

    def settle(self, customer_id: UUID, session_id: UUID) -> None:
        """The session's probe was answered, or the session ended: nothing waits any more."""
        sessions = self._waiting.get(customer_id)
        if sessions is not None:
            sessions.pop(session_id, None)
            if not sessions:
                del self._waiting[customer_id]

    def _put(self, sub: PhoneSubscription, event: PhoneEvent) -> None:
        if sub.closed:
            return
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            sub.closed = True
            self.unsubscribe(sub)
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.get_nowait()  # make room for the close marker
                sub.queue.put_nowait(None)


def _customer_of(sessions: sessionmaker[Session], payment_id: UUID) -> UUID:
    with sessions() as s:
        payment = bank.get_payment(s, payment_id)
    if payment is None or payment.payer_customer_id is None:
        raise NotFoundError(f"No customer for payment {payment_id}.")
    return payment.payer_customer_id


def probe_requested(issued: IssuedProbe) -> dict[str, Any]:
    """docs/08 §4's ``probe.requested``. A question carries its served (APPROVED, FR-KB-05)
    variant; the other screens are fixed in code and carry no text."""
    q = issued.question
    return {
        "type": PROBE_REQUESTED,
        "session_id": str(issued.session.id),
        "probe_run_id": str(issued.probe_run_id),
        "probe_key": issued.probe_key,
        "ui": issued.ui.value,
        "language": q.language if q else None,
        "variant_id": q.variant_id if q else None,
        "text": q.text if q else None,
        "options": [{"key": o.key, "text": o.text} for o in q.options] if q else [],
        "timeout_s": issued.timeout_s,
    }


def session_decided(record: GuardianSessionRecord) -> dict[str, Any]:
    """The session ended: its decision, or ``null`` with status CANCELLED_BY_CUSTOMER."""
    return {
        "type": SESSION_DECIDED,
        "session_id": str(record.id),
        "status": record.status.value,
        "decision": record.decision,
        "terminal_reason": record.terminal_reason.value if record.terminal_reason else None,
    }


class PhoneChannel:
    """The ``CustomerChannel`` of LIVE sessions: the probe goes to the payer's phone stream."""

    def __init__(self, hub: PhoneHub, sessions: sessionmaker[Session]) -> None:
        self._hub = hub
        self._sessions = sessions
        self._customers: dict[UUID, UUID] = {}  # payment → its payer customer

    async def customer(self, payment_id: UUID) -> UUID:
        known = self._customers.get(payment_id)
        if known is None:
            known = await asyncio.to_thread(_customer_of, self._sessions, payment_id)
            self._customers[payment_id] = known
        return known

    async def ask(self, issued: IssuedProbe) -> None:
        customer = await self.customer(issued.session.payment_id)
        session_id = issued.session.id
        self._hub.publish(
            customer, session_id, PhoneEvent(PROBE_REQUESTED, probe_requested(issued)), waiting=True
        )
        if issued.otp_code is not None:  # the mock SMS (docs/08 §4 OTP protocol step 2)
            sms = {
                "type": OTP_SMS,
                "session_id": str(session_id),
                "probe_run_id": str(issued.probe_run_id),
                "code": issued.otp_code,
            }
            self._hub.publish(customer, session_id, PhoneEvent(OTP_SMS, sms), waiting=True)

    async def finished(self, record: GuardianSessionRecord) -> None:
        customer = await self.customer(record.payment_id)
        self._hub.settle(customer, record.id)
        self._hub.publish(customer, record.id, PhoneEvent(SESSION_DECIDED, session_decided(record)))
        self._customers.pop(record.payment_id, None)

    async def attempt_result(
        self, record: GuardianSessionRecord, probe_run_id: UUID, attempts_remaining: int
    ) -> None:
        customer = await self.customer(record.payment_id)
        data = {
            "type": PROBE_ATTEMPT_RESULT,
            "session_id": str(record.id),
            "probe_run_id": str(probe_run_id),
            "attempts_remaining": attempts_remaining,
        }
        self._hub.publish(customer, record.id, PhoneEvent(PROBE_ATTEMPT_RESULT, data))

    async def answered(self, record: GuardianSessionRecord) -> None:
        self._hub.settle(await self.customer(record.payment_id), record.id)


def _phone_session(s: Session, session_id: UUID) -> None:
    """404 for an unknown session; 409 for one the worker drives (a simulated customer
    answers it, docs/08 §1), which the phone must not answer or cancel."""
    runner = s.execute(
        select(guardian_sessions.c.runner).where(guardian_sessions.c.id == session_id)
    ).scalar_one_or_none()
    if runner is None:
        raise NotFoundError("No Guardian session with this id.")
    if runner != Runner.API.value:
        raise DelphiError(
            "This session is driven by the simulation, not the phone.",
            code=ProblemCode.INVALID_STATE_TRANSITION,
        )


def _run_of(sessions: sessionmaker[Session], session_id: UUID, probe_run_id: UUID) -> None:
    """404 unless the run is the session's; PROBE_NOT_AWAITING (409) unless it waits."""
    with sessions() as s:
        _phone_session(s, session_id)
        status = s.execute(
            select(probe_runs.c.status).where(
                probe_runs.c.id == probe_run_id, probe_runs.c.session_id == session_id
            )
        ).scalar_one_or_none()
    if status is None:
        raise NotFoundError("This session has no probe run with this id.")
    if status != ProbeRunStatus.AWAITING_CUSTOMER.value:
        raise DelphiError(
            "This probe is not waiting for an answer.", code=ProblemCode.PROBE_NOT_AWAITING
        )


def _awaited_run(sessions: sessionmaker[Session], session_id: UUID) -> UUID | None:
    """The session's waiting probe run, if any."""
    with sessions() as s:
        _phone_session(s, session_id)
        found: UUID | None = s.execute(
            select(probe_runs.c.id).where(
                probe_runs.c.session_id == session_id,
                probe_runs.c.status == ProbeRunStatus.AWAITING_CUSTOMER.value,
            )
        ).scalar_one_or_none()
    return found


@dataclass
class LiveGuardian:
    """The api's runner slot: LIVE sessions for the mock phone, driven in this process."""

    service: GuardianService
    runner: SessionRunner
    board: AnswerBoard
    phone: PhoneChannel
    hub: PhoneHub
    sessions: sessionmaker[Session]

    async def start(self, payment_id: UUID) -> GuardianSessionRecord:
        """docs/08 §2 steps 1–3 now; the rest runs in the session's task."""
        return await self.runner.submit(payment_id, mode=SessionMode.LIVE)

    async def respond(
        self,
        session_id: UUID,
        probe_run_id: UUID,
        answer: Mapping[str, Any],
        signals: ClientSignals,
    ) -> Responded:
        """Record the phone's answer, then wake the session's task. A wrong OTP with attempts
        left also goes to the phone as ``probe.attempt_result``."""
        await asyncio.to_thread(_run_of, self.sessions, session_id, probe_run_id)
        reply = await asyncio.to_thread(self.service.respond, probe_run_id, answer, signals)
        if reply.completed:
            await self.phone.answered(reply.session)
            self.board.answered(probe_run_id)
        elif reply.attempts_remaining is not None:
            await self.phone.attempt_result(reply.session, probe_run_id, reply.attempts_remaining)
        return reply

    async def abandon(self) -> int:
        """Drop the LIVE sessions this process is driving, before a demo reset deletes them
        (docs/14 §10): their tasks stop and phones that connect later are not shown their
        probes. Returns how many it dropped."""
        dropped = await self.runner.shutdown()
        self.hub.forget_waiting()
        return dropped

    async def cancel(self, session_id: UUID) -> GuardianSessionRecord:
        """The customer cancelled (FR-GDN-07). The waiting task is woken at once (docs/08 §2)."""
        waiting = await asyncio.to_thread(_awaited_run, self.sessions, session_id)
        record = await asyncio.to_thread(self.service.cancel, session_id)
        if waiting is not None:
            self.board.answered(waiting)
        return record


def live_guardian(
    service: GuardianService, sessions: sessionmaker[Session], clock: Clock
) -> LiveGuardian:
    """Wire the slot: the phone channel, the board and an API runner around ``service``."""
    hub = PhoneHub()
    board = AnswerBoard()
    channel = PhoneChannel(hub, sessions)
    runner = SessionRunner(
        service, channel, board, sessions, clock, runner=Runner.API, on_finished=channel.finished
    )
    return LiveGuardian(service, runner, board, channel, hub, sessions)
