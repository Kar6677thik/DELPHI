"""Customer channels for the session runner (docs/08 §4, #95).

``SimCustomerChannel`` answers for simulated customers in the worker. It asks an ``Answerer``
(``delphi.sim.customer.SimulatedCustomer``, wired in by the worker's composition root, so the
Guardian never imports code that reads ground truth) for the raw answer, waits the customer's
answering time, then answers through ``GuardianService.respond`` and the ``AnswerBoard``: the
same contract as the mock phone's probe-responses handler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from delphi.guardian.active import ClientSignals
from delphi.guardian.runner import AnswerBoard
from delphi.guardian.service import GuardianService, IssuedProbe
from delphi.platform.clock import Clock
from delphi.platform.logging import get_logger

log = get_logger(__name__)
MS_PER_S = 1000


class Answerer(Protocol):
    def answer(
        self,
        *,
        payment_id: UUID,
        probe_key: str,
        options: Sequence[str],
        otp_code: str | None,
        timeout_s: float,
        at: datetime,
    ) -> Sequence[tuple[Mapping[str, Any], float, int]]: ...


Wait = Callable[[float], Awaitable[None]]


class SimCustomerChannel:
    """Answers each issued probe in the background (``ask`` returns at once)."""

    def __init__(
        self,
        service: GuardianService,
        board: AnswerBoard,
        answerer: Answerer,
        clock: Clock,
        *,
        wait: Wait = asyncio.sleep,
    ) -> None:
        self._service = service
        self._board = board
        self._answerer = answerer
        self._clock = clock
        self._wait = wait  # the customer's answering time; tests move a fixed clock instead
        self._tasks: set[asyncio.Task[None]] = set()

    async def ask(self, issued: IssuedProbe) -> None:
        task = asyncio.create_task(self._answer(issued), name=f"sim-answer-{issued.probe_run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, issued: IssuedProbe) -> None:
        options = [o.key for o in issued.question.options] if issued.question else []
        steps = await asyncio.to_thread(
            self._answerer.answer,
            payment_id=issued.session.payment_id,
            probe_key=issued.probe_key,
            options=options,
            otp_code=issued.otp_code,
            timeout_s=issued.timeout_s,
            at=self._clock.now(),
        )
        try:
            for answer, latency_s, switches in steps:
                shown_ms = int(self._clock.now().timestamp() * MS_PER_S)
                await self._wait(latency_s)
                signals = ClientSignals(shown_ms, shown_ms + round(latency_s * MS_PER_S), switches)
                reply = await asyncio.to_thread(
                    self._service.respond, issued.probe_run_id, answer, signals
                )
                if reply.completed:
                    break
        except Exception as exc:  # e.g. the session ended meanwhile: the runner moves on
            log.warning("guardian.sim_answer_failed", run=str(issued.probe_run_id), exc_info=exc)
        finally:
            self._board.answered(issued.probe_run_id)
