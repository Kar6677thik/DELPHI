"""The worker's Guardian runner (docs/04 §5.1, docs/08 §1–2, FR-GDN-14, #95).

Simulated customers answer AUTOPLAY sessions in the worker, the only process with ground truth.
``WorkerGuardian`` is the worker's runner slot: a ``SessionRunner`` with runner ``WORKER`` whose
channel is the ``SimCustomerChannel``, on an event loop of its own thread (the worker's job
slots are threads). At start it resumes every unfinished WORKER session (FR-GDN-14); a job
(``DEMO_STORY``) submits a payment from its slot thread with ``submit``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from delphi.guardian.channels import Answerer, SimCustomerChannel, Wait
from delphi.guardian.runner import AnswerBoard, SessionRunner
from delphi.guardian.service import GuardianService
from delphi.guardian.store import GuardianSessionRecord, Runner, SessionMode
from delphi.guardian.tables import guardian_sessions
from delphi.platform.clock import Clock


class WorkerGuardian:
    """AUTOPLAY sessions, driven on the worker's Guardian loop."""

    def __init__(
        self,
        runner: SessionRunner,
        channel: SimCustomerChannel,
        sessions: sessionmaker[Session],
    ) -> None:
        self.runner = runner
        self.channel = channel
        self._sessions = sessions
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Lock()

    def start(self) -> list[UUID]:
        """Start the loop and resume the sessions a restart interrupted. Returns their ids."""
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="guardian-worker", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        return self._call(self.runner.resume_all())

    def submit(self, payment_id: UUID) -> UUID:
        """Start the payment's AUTOPLAY session (steps 1–3 now, the rest in its task) and return
        its id. A payment that already has a session returns that one: a job retried after a
        lost lease never starts a second (one session per payment: two attempts at once cannot
        both start one, and the loser's retry finds the winner's)."""
        known = self._session_of(payment_id)
        if known is not None:
            return known
        record: GuardianSessionRecord = self._call(
            self.runner.submit(payment_id, mode=SessionMode.AUTOPLAY)
        )
        return record.id

    def abandon(self) -> int:
        """Drop the sessions this runner is driving and the simulated answers on their way,
        and keep running (the demo reset, docs/14 §10, deletes those sessions next). Returns
        how many it dropped; none when the runner is not started."""
        if self._loop is None:
            return 0
        return self._call(self._shutdown())

    def stop(self) -> None:
        """Stop the session tasks and the simulated answers; they resume on the next start.
        Safe to call twice, from two threads."""
        with self._stopping:
            loop, thread = self._loop, self._thread
            if loop is None or thread is None:
                return
            self._call(self._shutdown())
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()
            self._loop = self._thread = None

    async def _shutdown(self) -> int:
        dropped = await self.runner.shutdown()
        await self.channel.aclose()
        return dropped

    def _call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        if self._loop is None:
            coro.close()
            raise RuntimeError("the worker's Guardian runner is not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _session_of(self, payment_id: UUID) -> UUID | None:
        with self._sessions() as s:
            found: UUID | None = s.execute(
                select(guardian_sessions.c.id).where(guardian_sessions.c.payment_id == payment_id)
            ).scalar_one_or_none()
        return found


def worker_guardian(
    service: GuardianService,
    answerer: Answerer,
    sessions: sessionmaker[Session],
    clock: Clock,
    *,
    wait: Wait = asyncio.sleep,
) -> WorkerGuardian:
    """Wire the worker's slot: simulated customers answer through ``SimCustomerChannel``.
    ``wait`` is the customer's answering time (tests move a fixed clock instead)."""
    board = AnswerBoard()
    channel = SimCustomerChannel(service, board, answerer, clock, wait=wait)
    runner = SessionRunner(service, channel, board, sessions, clock, runner=Runner.WORKER)
    return WorkerGuardian(runner, channel, sessions)
