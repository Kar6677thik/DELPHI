"""#229: the stored `dd_pack_complete` must not miss a disposition that races the case's last
narrative (docs/09 §7). Two connections interleave the disposition and the narrative write, in
both orders; whichever commits second must see the other and store `true`."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from uuid import UUID

import pytest

from delphi.guardian.api import case_trail
from delphi.navigator.api import (
    Disposition,
    dispose,
    get_case,
    refresh_dd_pack_complete,
    request_evidence,
)
from delphi.navigator.jobs import write_narrative

from .test_case_ddpack import _investigator, _s1_case
from .test_guardian_service import Env, make_env

# How long the second transaction is given to reach its lock before the first commits.
BLOCK_S = 0.5
JOIN_S = 30.0


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    yield from make_env(create_database)


def _decided_case_with_one_record(env: Env) -> tuple[UUID, UUID, UUID]:
    """An S-1 case with one evidence record still waiting for its narrative."""
    case_id = _s1_case(env)
    user = _investigator(env)
    with env.sessions.begin() as s:
        record = request_evidence(s, env.clock, case_id, "ACCOUNT_PROFILE", requested_by=user)
    return case_id, record.id, user


def _dispose(env: Env, case_id: UUID, user: UUID) -> None:
    """The disposition route's transaction body: decide, then recompute the flag."""
    with env.sessions.begin() as s:
        case = get_case(s, case_id)
        assert case is not None
        _dispose_in(s, env, case_id, user, case.version, case.recommended_decision)


def _dispose_in(
    s: object, env: Env, case_id: UUID, user: UUID, version: int, recommended: str | None
) -> None:
    dispose(
        s,  # type: ignore[arg-type]
        env.clock,
        case_id,
        Disposition(
            decision="CONFIRM_FRAUD",
            confidence=4,
            typology="APP_DIGITAL_ARREST",
            note=None,
            override_rationale=(
                None
                if recommended in (None, "CONFIRM_FRAUD")
                else "The customer confirmed the caller coached them on the call."
            ),
        ),
        expected_version=version,
        user_id=user,
    )
    refresh_dd_pack_complete(s, case_id, case_trail)  # type: ignore[arg-type]


def _narrate(env: Env, record_id: UUID) -> None:
    with env.sessions.begin() as s:
        write_narrative(
            s,
            record_id,
            text=f"The payee account is new [{record_id}].",
            source="TEMPLATE",
            citations=[str(record_id)],
            trail=case_trail,
        )


def _in_thread(work: Callable[[], None]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as exc:  # surfaced by the test, not swallowed
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _stored_flag(env: Env, case_id: UUID) -> bool:
    with env.sessions() as s:
        case = get_case(s, case_id)
        assert case is not None
        return case.dd_pack_complete


def test_a_narrative_written_while_the_disposition_is_open_still_completes_the_flag(
    env: Env,
) -> None:
    """The disposition is in flight (not committed) when the last narrative is written."""
    case_id, record_id, user = _decided_case_with_one_record(env)
    with env.sessions.begin() as s:
        case = get_case(s, case_id)
        assert case is not None
        _dispose_in(s, env, case_id, user, case.version, case.recommended_decision)
        # The narrative job runs now, against a case that is not yet decided for anyone else.
        thread, errors = _in_thread(lambda: _narrate(env, record_id))
        time.sleep(BLOCK_S)
    thread.join(JOIN_S)
    assert not thread.is_alive() and not errors, errors
    assert _stored_flag(env, case_id) is True


def test_a_disposition_made_while_the_narrative_is_open_still_completes_the_flag(
    env: Env,
) -> None:
    """The last narrative is in flight (not committed) when the case is decided. Before #229 this
    order held only by accident: publishing `evidence.narrative_ready` takes the outbox's advisory
    lock, which the disposition's `case.updated` waits on. Now the case row lock orders it."""
    case_id, record_id, user = _decided_case_with_one_record(env)
    with env.sessions.begin() as s:
        write_narrative(
            s,
            record_id,
            text=f"The payee account is new [{record_id}].",
            source="TEMPLATE",
            citations=[str(record_id)],
            trail=case_trail,
        )
        thread, errors = _in_thread(lambda: _dispose(env, case_id, user))
        time.sleep(BLOCK_S)
    thread.join(JOIN_S)
    assert not thread.is_alive() and not errors, errors
    assert _stored_flag(env, case_id) is True
