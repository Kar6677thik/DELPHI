"""Running Guardian sessions (docs/04 §5.1, docs/08 §2, FR-GDN-14, #95).

Each session's steps run as **one asyncio task** in the process that owns it
(`guardian_sessions.runner`: API for the mock phone, WORKER for simulated customers). The task
drives ``GuardianService`` step by step; every step commits, so the task can die anywhere and
``resume_all`` picks the session up again on the next start-up:

- CREATED / PASSIVE_PROBING → the passive phase (re-run safely, docs/08 §2)
- PLANNING                  → plan (a PENDING run chosen before the crash is reused) and issue
- AWAITING_CUSTOMER         → wait out the rest of the run's deadline, then time it out

Answers do not come through the task. Whoever receives one (the api's probe-responses handler
for the phone, the simulated customer in the worker) calls ``GuardianService.respond`` itself,
so validation errors reach the caller, and then signals the ``AnswerBoard``; the waiting task
wakes and plans the next step. A deadline that passes first makes the task call ``timeout``.

The service's steps are synchronous (SQLAlchemy Core); the task runs them in threads, so the
event loop never blocks on the database. The passive phase is already async.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from delphi.guardian.service import (
    DONE,
    Decided,
    GuardianService,
    IssuedProbe,
    NextProbe,
)
from delphi.guardian.store import (
    GuardianSessionRecord,
    ProbeRunStatus,
    Runner,
    SessionMode,
    SessionStatus,
    get_session,
)
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.logging import get_logger

log = get_logger(__name__)


class CustomerChannel(Protocol):
    """Puts an issued probe in front of the customer (docs/08 §4): the mock phone's SSE stream,
    or a simulated customer. It does not wait for the answer."""

    async def ask(self, issued: IssuedProbe) -> None: ...


@dataclass
class AnswerBoard:
    """An in-process rendezvous between the code that records an answer and the session task
    waiting for it, keyed by probe run. The runner ``expect``s a run before asking, so an answer
    that arrives before it starts waiting is not lost; a signal for a run nobody expects (a
    stray, a late answer after the session ended) is dropped, so nothing accumulates."""

    _events: dict[UUID, asyncio.Event] = field(default_factory=dict)

    def expect(self, probe_run_id: UUID) -> None:
        self._events.setdefault(probe_run_id, asyncio.Event())

    def answered(self, probe_run_id: UUID) -> None:
        """Called once the answer is recorded, or the session ended (e.g. a cancel)."""
        event = self._events.get(probe_run_id)
        if event is not None:
            event.set()

    async def wait(self, probe_run_id: UUID, timeout_s: float) -> bool:
        """True if signalled within ``timeout_s`` seconds. Forgets the run either way."""
        event = self._events.setdefault(probe_run_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), max(timeout_s, 0.0))
            return True
        except TimeoutError:
            return False
        finally:
            self._events.pop(probe_run_id, None)


Finished = Callable[[GuardianSessionRecord], Awaitable[None]]


class SessionRunner:
    """Owns the session tasks of one process (one ``Runner`` kind). ``on_finished`` hears about
    every session that ends here (decided, cancelled, or the fail-safe): the mock phone's
    ``session.decided`` (docs/08 §4). It never affects the session."""

    def __init__(
        self,
        service: GuardianService,
        channel: CustomerChannel,
        board: AnswerBoard,
        sessions: sessionmaker[Session],
        clock: Clock,
        *,
        runner: Runner,
        on_finished: Finished | None = None,
    ) -> None:
        self._service = service
        self._channel = channel
        self._board = board
        self._sessions = sessions
        self._clock = clock
        self._runner = runner
        self._on_finished = on_finished
        self._tasks: dict[UUID, asyncio.Task[GuardianSessionRecord]] = {}

    # ------------------------------------------------------------------ entry points

    async def submit(self, payment_id: UUID, *, mode: SessionMode) -> GuardianSessionRecord:
        """Start a session for a payment and drive it in its own task. Returns the session as
        created (or already decided, on the fast path); the task carries on in the background."""
        record = await _off_loop(
            lambda: self._service.start(payment_id, mode=mode, runner=self._runner)
        )
        if record.status not in DONE:
            self._spawn(record.id)
        else:
            await self._finished(record)  # the fast path, or the fail-safe at the prior
        return record

    async def resume_all(self) -> list[UUID]:
        """FR-GDN-14: drive every unfinished session this process owns (docs/04 §5.1)."""
        with self._sessions() as s:
            ids = list(
                s.execute(
                    select(guardian_sessions.c.id).where(
                        guardian_sessions.c.runner == self._runner.value,
                        guardian_sessions.c.status.not_in([d.value for d in DONE]),
                    )
                ).scalars()
            )
        for session_id in ids:
            self._spawn(session_id)
        log.info("guardian.sessions_resumed", runner=self._runner.value, count=len(ids))
        return ids

    def task(self, session_id: UUID) -> asyncio.Task[GuardianSessionRecord] | None:
        return self._tasks.get(session_id)

    async def shutdown(self) -> int:
        """Stop every task; their sessions are resumed on the next start-up. The runner can
        take new sessions afterwards (the demo reset drops the ones it is driving). Returns how
        many tasks it stopped."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        return len(tasks)

    # ------------------------------------------------------------------ the session task

    def _spawn(self, session_id: UUID) -> None:
        if session_id in self._tasks and not self._tasks[session_id].done():
            return  # already driven by this process
        task = asyncio.create_task(self.drive(session_id), name=f"guardian-{session_id}")
        self._tasks[session_id] = task
        task.add_done_callback(lambda done: self._forget(session_id, done))

    def _forget(self, session_id: UUID, done: asyncio.Task[GuardianSessionRecord]) -> None:
        if self._tasks.get(session_id) is done:  # never a newer task for the same session
            del self._tasks[session_id]

    async def drive(self, session_id: UUID) -> GuardianSessionRecord:
        """Run the session to its end from wherever it is (docs/08 §2 steps 4–9). Anything
        unexpected ends it through the fail-safe (FR-GDN-13); a cancelled task just stops, and
        the session is resumed on the next start-up."""
        try:
            record = await self._drive(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc  # keep it past the except block for the thread
            record = await _off_loop(lambda: self._service.fail_safe(session_id, error))
        await self._finished(record)
        return record

    async def _finished(self, record: GuardianSessionRecord) -> None:
        if self._on_finished is None:
            return
        try:
            await self._on_finished(record)
        except Exception as exc:  # telling the customer must never change the session
            log.warning("guardian.on_finished_failed", session=str(record.id), exc_info=exc)

    async def _drive(self, session_id: UUID) -> GuardianSessionRecord:
        while True:
            current = await _off_loop(lambda: self._session(session_id))
            if current.status in DONE:
                return current
            if current.status in (SessionStatus.CREATED, SessionStatus.PASSIVE_PROBING):
                await self._service.passive(session_id)
            elif current.status is SessionStatus.PLANNING:
                await self._plan_and_ask(session_id)
            else:  # AWAITING_CUSTOMER: an answer, or the deadline
                await self._await_answer(session_id)

    async def _plan_and_ask(self, session_id: UUID) -> None:
        step = await _off_loop(lambda: self._service.plan(session_id))
        if isinstance(step, Decided):
            return
        assert isinstance(step, NextProbe)
        issued = await _off_loop(lambda: self._service.issue(step.probe_run_id))
        if isinstance(issued, IssuedProbe):
            self._board.expect(issued.probe_run_id)  # before asking: a fast answer is not lost
            await self._channel.ask(issued)

    async def _await_answer(self, session_id: UUID) -> None:
        waiting = await _off_loop(lambda: self._awaited(session_id))
        if waiting is None:
            current = await _off_loop(lambda: self._session(session_id))
            if current.status is SessionStatus.AWAITING_CUSTOMER:
                # waiting with nothing to wait for can only be a bug: never loop on it
                raise RuntimeError(f"session {session_id} awaits no probe run")
            return  # the run was answered or closed meanwhile: the loop re-reads
        run_id, deadline = waiting
        self._board.expect(run_id)
        if await _off_loop(lambda: self._awaited(session_id)) != waiting:
            self._board.answered(run_id)  # answered between the two reads: do not wait
        remaining = (deadline - self._clock.now()).total_seconds()
        if await self._board.wait(run_id, remaining):
            return  # the answer is recorded; the next loop plans
        if self._clock.now() < deadline:
            return  # the timer fired a little early: the loop waits out the rest
        try:
            await _off_loop(lambda: self._service.timeout(run_id))
        except DelphiError as exc:
            if exc.code is not ProblemCode.INVALID_STATE_TRANSITION:
                raise
            # answered (or cancelled) just as the deadline passed: nothing to time out

    # ------------------------------------------------------------------ reads

    def _session(self, session_id: UUID) -> GuardianSessionRecord:
        with self._sessions() as s:
            current = get_session(s, session_id)
        if current is None:
            raise NotFoundError(f"No Guardian session {session_id}.")
        return current

    def _awaited(self, session_id: UUID) -> tuple[UUID, datetime] | None:
        with self._sessions() as s:
            row = s.execute(
                select(probe_runs.c.id, probe_runs.c.raw_response).where(
                    probe_runs.c.session_id == session_id,
                    probe_runs.c.status == ProbeRunStatus.AWAITING_CUSTOMER.value,
                )
            ).one_or_none()
        if row is None:
            return None
        return row.id, datetime.fromisoformat(row.raw_response["deadline_at"])


async def _off_loop[T](step: Callable[[], T]) -> T:
    """A synchronous service step, off the event loop."""
    return await asyncio.to_thread(step)
