"""The Guardian session store on a real database (docs/05 §2, docs/17 §3, #94 part 1): sessions
pinned to the ACTIVE KB, one per payment, numbered probe runs, and the invariants the migration
enforces."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from delphi.guardian.store import (
    ProbeMode,
    ProbeRunStatus,
    Runner,
    SelectedBy,
    SessionExistsError,
    SessionMode,
    SessionStatus,
    add_probe_run,
    create_session,
    get_session,
    list_probe_runs,
    session_for_payment,
)
from delphi.guardian.tables import guardian_sessions, metadata, probe_runs
from delphi.kb.loader import load_and_validate
from delphi.kb.store import ensure_kb_v1
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.errors import DelphiError, NotFoundError
from delphi.probes.registry import CLASSIFIER_NAMES

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
KB_DIR = Path(__file__).resolve().parents[3] / "kb"


@dataclass
class Env:
    owner: Engine
    sessions: sessionmaker[Session]
    clock: FixedClock
    kb_version_id: int

    def payment(self) -> UUID:
        """A minimal payment row (the session's foreign key needs one)."""
        customer, payer, payee, payment = uuid4(), uuid4(), uuid4(), uuid4()
        with self.owner.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO bank.customers (id, display_name, age, language, is_vulnerable,"
                    " segment, income_band, consent_call_status, home_city)"
                    " VALUES (:c, 'CUST', 68, 'te', false, 'RETAIL', 'MID', true, 'Vijayawada')"
                ),
                {"c": customer},
            )
            for account, owner in ((payer, customer), (payee, None)):
                conn.execute(
                    text(
                        "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at,"
                        " kyc_level, status, balance_paise)"
                        " VALUES (:a, :c, 'DLPH', :t, 'FULL', 'ACTIVE', 0)"
                    ),
                    {"a": account, "c": owner, "t": T0},
                )
            conn.execute(
                text(
                    "INSERT INTO bank.payments (id, payer_account_id, payee_account_id,"
                    " amount_paise, channel, initiated_at, status, origin)"
                    " VALUES (:p, :a, :b, 48000000, 'UPI_PUSH', :t, 'PENDING_GUARDIAN', 'DEMO')"
                ),
                {"p": payment, "a": payer, "b": payee, "t": T0},
            )
        return payment

    def new_session(self, payment_id: UUID | None = None) -> UUID:
        with self.sessions.begin() as s:
            record = create_session(
                s,
                self.clock,
                payment_id=payment_id or self.payment(),
                mode=SessionMode.LIVE,
                runner=Runner.API,
                rng_seed=7,
            )
        return record.id


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    url = create_database()
    upgrade(url)
    owner = engine_as(url)
    app = engine_as(url, "delphi_app")  # the api's role: its grants are exercised
    clock = FixedClock(T0)
    with make_session_factory(owner).begin() as s:
        kb_version_id = ensure_kb_v1(s, clock, load_and_validate(KB_DIR, CLASSIFIER_NAMES))
    assert kb_version_id is not None
    yield Env(owner, make_session_factory(app), clock, kb_version_id)
    app.dispose()
    owner.dispose()


def test_the_table_descriptions_match_the_migrated_schema(env: Env) -> None:
    insp = inspect(env.owner)
    for table in metadata.tables.values():
        reflected = [c["name"] for c in insp.get_columns(table.name, schema="delphi")]
        assert reflected == [c.name for c in table.columns], table.name


def test_a_session_is_created_and_pinned_to_the_active_kb(env: Env) -> None:
    payment = env.payment()
    session_id = env.new_session(payment)
    with env.sessions() as s:
        record = get_session(s, session_id)
        assert session_for_payment(s, payment) == record
    assert record is not None
    assert record.status is SessionStatus.CREATED and record.decision is None
    assert record.kb_version_id == env.kb_version_id
    assert (record.mode, record.runner, record.rng_seed) == (SessionMode.LIVE, Runner.API, 7)
    assert record.started_at == T0 and record.version == 1


def test_one_session_per_payment(env: Env) -> None:
    payment = env.payment()
    env.new_session(payment)
    with pytest.raises(SessionExistsError):
        env.new_session(payment)


def test_no_session_without_an_active_kb(env: Env) -> None:
    with env.owner.begin() as conn:
        conn.execute(text("UPDATE delphi.kb_versions SET status = 'RETIRED'"))
    with pytest.raises(DelphiError, match="No ACTIVE KB"):
        env.new_session()


def test_probe_runs_are_numbered_in_order(env: Env) -> None:
    session_id = env.new_session()
    with env.sessions.begin() as s:
        for key in ("CALL_STATUS", "DEVICE_POSTURE"):
            add_probe_run(s, env.clock, session_id, probe_key=key, mode=ProbeMode.PASSIVE)
        add_probe_run(
            s,
            env.clock,
            session_id,
            probe_key="PURPOSE_QUESTION",
            mode=ProbeMode.ACTIVE,
            status=ProbeRunStatus.AWAITING_CUSTOMER,
            selected_by=SelectedBy.GREEDY,
            propensity=0.95,
        )
    with env.sessions() as s:
        runs = list_probe_runs(s, session_id)
    assert [(r.seq, r.probe_key) for r in runs] == [
        (1, "CALL_STATUS"),
        (2, "DEVICE_POSTURE"),
        (3, "PURPOSE_QUESTION"),
    ]
    with pytest.raises(NotFoundError), env.sessions.begin() as s:
        add_probe_run(s, env.clock, uuid4(), probe_key="X", mode=ProbeMode.PASSIVE)


# --------------------------------------------- the docs/05 §2 invariants, as constraints


def _update_session(env: Env, session_id: UUID, **values: object) -> None:
    with env.owner.begin() as conn:
        conn.execute(
            update(guardian_sessions).where(guardian_sessions.c.id == session_id).values(**values)
        )


def test_decided_if_and_only_if_there_is_a_decision(env: Env) -> None:
    session_id = env.new_session()
    with pytest.raises(IntegrityError):
        _update_session(env, session_id, decision="RELEASE")  # not DECIDED
    with pytest.raises(IntegrityError):  # DECIDED without a decision
        _update_session(env, session_id, status="DECIDED", terminal_reason="STOP", decided_at=T0)
    _update_session(
        env,
        session_id,
        status="DECIDED",
        decision="RELEASE",
        terminal_reason="FAST_PATH",
        decided_at=T0,
    )


def test_terminal_reasons_follow_the_state_machine(env: Env) -> None:
    session_id = env.new_session()
    with pytest.raises(IntegrityError):  # a reason before the end
        _update_session(env, session_id, terminal_reason="STOP")
    with pytest.raises(IntegrityError):  # a decision cannot be "cancelled"
        _update_session(
            env,
            session_id,
            status="DECIDED",
            decision="RELEASE",
            terminal_reason="CUSTOMER_CANCELLED",
            decided_at=T0,
        )
    with pytest.raises(IntegrityError):  # a cancellation has its own reason
        _update_session(
            env, session_id, status="CANCELLED_BY_CUSTOMER", terminal_reason="STOP", decided_at=T0
        )
    with pytest.raises(IntegrityError):  # an ended session has an end time
        _update_session(
            env, session_id, status="CANCELLED_BY_CUSTOMER", terminal_reason="CUSTOMER_CANCELLED"
        )
    _update_session(
        env,
        session_id,
        status="CANCELLED_BY_CUSTOMER",
        terminal_reason="CUSTOMER_CANCELLED",
        decided_at=T0,
    )


def test_at_most_one_probe_awaits_the_customer(env: Env) -> None:
    session_id = env.new_session()
    with env.sessions.begin() as s:
        add_probe_run(
            s,
            env.clock,
            session_id,
            probe_key="PURPOSE_QUESTION",
            mode=ProbeMode.ACTIVE,
            status=ProbeRunStatus.AWAITING_CUSTOMER,
            selected_by=SelectedBy.GREEDY,
        )
    with pytest.raises(IntegrityError), env.sessions.begin() as s:
        add_probe_run(
            s,
            env.clock,
            session_id,
            probe_key="SPELL_BREAKER_DIGITAL_ARREST",
            mode=ProbeMode.ACTIVE,
            status=ProbeRunStatus.AWAITING_CUSTOMER,
            selected_by=SelectedBy.EXPLORE,
        )


def test_a_probe_runs_once_per_session_and_passive_probes_are_not_selected(env: Env) -> None:
    session_id = env.new_session()
    with env.sessions.begin() as s:
        add_probe_run(s, env.clock, session_id, probe_key="CALL_STATUS", mode=ProbeMode.PASSIVE)
    with pytest.raises(IntegrityError), env.sessions.begin() as s:
        add_probe_run(s, env.clock, session_id, probe_key="CALL_STATUS", mode=ProbeMode.PASSIVE)
    with pytest.raises(IntegrityError), env.sessions.begin() as s:
        add_probe_run(
            s,
            env.clock,
            session_id,
            probe_key="DEVICE_POSTURE",
            mode=ProbeMode.PASSIVE,
            selected_by=SelectedBy.GREEDY,
        )


def test_a_completed_run_has_an_outcome(env: Env) -> None:
    session_id = env.new_session()
    with env.sessions.begin() as s:
        run = add_probe_run(
            s, env.clock, session_id, probe_key="CALL_STATUS", mode=ProbeMode.PASSIVE
        )
    with pytest.raises(IntegrityError), env.owner.begin() as conn:
        conn.execute(update(probe_runs).where(probe_runs.c.id == run.id).values(status="COMPLETED"))
    with env.owner.begin() as conn:
        conn.execute(
            update(probe_runs)
            .where(probe_runs.c.id == run.id)
            .values(status="COMPLETED", outcome="ON_CALL_UNKNOWN_NUMBER", completed_at=T0)
        )
