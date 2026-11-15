"""Running Guardian sessions as asyncio tasks, and resuming them after a crash (docs/04 §5.1,
docs/08 §2, FR-GDN-14, #95), on a real database."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from delphi.guardian.runner import AnswerBoard, SessionRunner
from delphi.guardian.service import GuardianService, IssuedProbe
from delphi.guardian.store import Runner, SessionMode, SessionStatus, TerminalReason

from .test_guardian_active import (
    KAMALA_ANSWERS,
    KAMALA_PASSIVE,
    KAMALA_PRIOR,
    signals,
    story_prior,
)
from .test_guardian_service import Env, make_env

Script = Mapping[str, tuple[dict[str, Any], float, int]]


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    yield from make_env(create_database)


class ScriptedCustomer:
    """A channel whose customer answers from a script, through the service, then signals the
    board: the same path as the phone's probe-responses handler (docs/08 §4)."""

    def __init__(self, env: Env, service: GuardianService, board: AnswerBoard, script: Script):
        self.env, self.service, self.board, self.script = env, service, board, script
        self.asked: list[str] = []

    async def ask(self, issued: IssuedProbe) -> None:
        self.asked.append(issued.probe_key)
        answer, latency, switches = self.script[issued.probe_key]
        sig = signals(self.env, latency, switches)
        await asyncio.to_thread(self.service.respond, issued.probe_run_id, answer, sig)
        self.board.answered(issued.probe_run_id)


class SilentCustomer:
    """A channel whose customer never answers (the phone was closed)."""

    def __init__(self) -> None:
        self.issued: asyncio.Queue[IssuedProbe] = asyncio.Queue()

    async def ask(self, issued: IssuedProbe) -> None:
        await self.issued.put(issued)


def _runner(env: Env, service: GuardianService, channel: Any, board: AnswerBoard) -> SessionRunner:
    return SessionRunner(service, channel, board, env.sessions, env.clock, runner=Runner.API)


def test_a_session_runs_to_its_decision_in_its_own_task(env: Env) -> None:
    async def scenario() -> tuple[list[str], Any]:
        service = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
        board = AnswerBoard()
        customer = ScriptedCustomer(env, service, board, KAMALA_ANSWERS)
        runner = _runner(env, service, customer, board)
        created = await runner.submit(env.payment(480_000), mode=SessionMode.LIVE)
        task = runner.task(created.id)
        assert task is not None
        return customer.asked, await asyncio.wait_for(task, 60)

    asked, decided = asyncio.run(scenario())
    assert asked[:2] == ["PURPOSE_QUESTION", "SPELL_BREAKER_DIGITAL_ARREST"]
    assert decided.decision == "PROTECTIVE_HOLD"


def test_a_killed_session_task_resumes_and_completes(env: Env) -> None:
    """FR-GDN-14: kill the task while it waits for the customer, start again, finish."""
    payment = env.payment(480_000)

    async def first_life() -> tuple[UUID, IssuedProbe]:
        service = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
        silent = SilentCustomer()
        runner = _runner(env, service, silent, AnswerBoard())
        created = await runner.submit(payment, mode=SessionMode.LIVE)
        issued = await asyncio.wait_for(silent.issued.get(), 60)
        await runner.shutdown()  # the api process dies here
        return created.id, issued

    session_id, waiting = asyncio.run(first_life())
    assert waiting.probe_key == "PURPOSE_QUESTION"

    async def second_life() -> tuple[list[UUID], Any]:
        service = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
        board = AnswerBoard()
        customer = ScriptedCustomer(env, service, board, KAMALA_ANSWERS)
        runner = _runner(env, service, customer, board)
        resumed = await runner.resume_all()
        # the phone still shows the probe from before the restart; its answer arrives now
        answer, latency, switches = KAMALA_ANSWERS["PURPOSE_QUESTION"]
        sig = signals(env, latency, switches)
        await asyncio.to_thread(service.respond, waiting.probe_run_id, answer, sig)
        board.answered(waiting.probe_run_id)
        task = runner.task(session_id)
        assert task is not None
        return resumed, await asyncio.wait_for(task, 60)

    resumed, decided = asyncio.run(second_life())
    assert resumed == [session_id]
    assert decided.status is SessionStatus.DECIDED and decided.decision == "PROTECTIVE_HOLD"
    assert env.payment_row(payment).status == "HELD"


def test_a_probe_whose_deadline_passed_while_down_is_timed_out_on_resume(env: Env) -> None:
    payment = env.payment(480_000)

    async def first_life() -> IssuedProbe:
        service = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
        silent = SilentCustomer()
        runner = _runner(env, service, silent, AnswerBoard())
        await runner.submit(payment, mode=SessionMode.LIVE)
        issued = await asyncio.wait_for(silent.issued.get(), 60)
        await runner.shutdown()
        return issued

    stale = asyncio.run(first_life())
    env.clock.advance(timedelta(seconds=stale.timeout_s + 5))  # down longer than the deadline

    async def second_life() -> Any:
        service = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
        board = AnswerBoard()
        customer = ScriptedCustomer(env, service, board, KAMALA_ANSWERS)
        runner = _runner(env, service, customer, board)
        [session_id] = await runner.resume_all()
        task = runner.task(session_id)
        assert task is not None
        return customer.asked, await asyncio.wait_for(task, 60)

    asked, decided = asyncio.run(second_life())
    assert "PURPOSE_QUESTION" not in asked  # timed out, not asked again (each source runs once)
    assert decided.status is SessionStatus.DECIDED
    assert decided.terminal_reason in (TerminalReason.STOP, TerminalReason.BUDGET_EXHAUSTED)


def test_resume_after_a_crash_between_planning_and_issuing(env: Env) -> None:
    service = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
    started = service.start(env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API)
    asyncio.run(service.passive(started.id))
    chosen = service.plan(started.id)  # PENDING, never issued: the process died here

    async def resume() -> tuple[list[str], Any]:
        board = AnswerBoard()
        customer = ScriptedCustomer(env, service, board, KAMALA_ANSWERS)
        runner = _runner(env, service, customer, board)
        await runner.resume_all()
        task = runner.task(started.id)
        assert task is not None
        return customer.asked, await asyncio.wait_for(task, 60)

    asked, decided = asyncio.run(resume())
    assert asked[0] == getattr(chosen, "probe_key", None)  # the same probe, not a second one
    assert decided.decision == "PROTECTIVE_HOLD"


def test_the_answer_board_times_out_and_ignores_strays() -> None:
    async def scenario() -> tuple[bool, bool]:
        board = AnswerBoard()
        run = UUID(int=1)
        board.answered(UUID(int=2))  # nobody waits for it: harmless
        late = await board.wait(run, 0.01)
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, board.answered, run)
        on_time = await board.wait(run, 5)
        return late, on_time

    assert asyncio.run(scenario()) == (False, True)
