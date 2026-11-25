"""The demo reset (docs/14 §10, FR-SIM-08, docs/21 E2E-RESET, #101) on a real database seeded
with the `test` profile and its personas: a story runs, labels are delivered, a new KB version
goes ACTIVE, and the reset brings the bank back to the state right after the seed, so the story
runs again."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, column, func, select, table, text, update

from delphi.api.routers import demo as demo_router
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import KbStatus, ensure_kb_v1, insert_version, kb_versions
from delphi.platform.accounts import Role
from delphi.platform.audit import audit_log
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobQueue, JobRegistry, JobStatus, JobType, QueueConfig, enqueue
from delphi.platform.locks import DEMO_RESET_LOCK_KEY
from delphi.platform.tables import jobs
from delphi.platform.worker import run_job
from delphi.sim.bank import accounts, ledger_entries, sim_schedule
from delphi.sim.delivery import deliver_labels
from delphi.sim.jobs import register_sim_jobs
from delphi.sim.reset import KEPT_TABLES, RESET_TABLES, register_reset_job
from delphi.sim.truth import label_schedule

from ._db import engine_as, upgrade
from .test_demo_story import (
    WORKER,
    Bank,
    _client,
    _decided,
    _key,
    _run_story,
    _worker,
)
from .test_guardian_active import ARJUN_PRIOR, story_prior


class RunningClock(FixedClock):
    """The system clock, which a test can also move forward (the customer's answering time,
    the minutes between demo steps)."""

    def __init__(self) -> None:
        super().__init__(datetime.now(UTC))
        self._offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(UTC) + self._offset

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("time does not run backwards")
        self._offset += delta


@pytest.fixture
def seeded(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Bank]:
    """The story tests' seeded bank, on the running clock: the seed's marker is the clock's
    time, read inside the seed's transaction, so rows the database stamps (`created_at`
    defaults to now()) are never newer than it, as in production."""
    url = create_database()
    upgrade(url)
    owner, sim, app = engine_as(url), engine_as(url, "delphi_sim"), engine_as(url, "delphi_app")
    clock = RunningClock()
    with make_session_factory(owner).begin() as s:
        assert ensure_kb_v1(s, clock, kb) is not None
    sessions = make_session_factory(app)
    queue = JobQueue(sessions, clock, QueueConfig())
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    with sessions.begin() as s:
        enqueue(
            s,
            clock,
            JobType.GENERATE_DATASET,
            idempotency_key="seed",
            params={"profile": "test", "personas": True},
        )
    job = queue.claim(WORKER, frozenset({JobType.GENERATE_DATASET}))
    assert job is not None
    run_job(queue, registry, job, WORKER)
    yield Bank(url, owner, sim, app, sessions, clock, queue)
    for engine in (owner, sim, app):
        engine.dispose()


RESET = "/api/v1/admin/demo/reset"
STORIES = "/api/v1/admin/demo/scenarios"
SESSIONS = "/api/v1/guardian/sessions"
SCHEMAS = ("bank", "delphi", "sim_truth")
TIMEOUT_S = 60.0


def _table(name: str) -> Any:
    schema, table_name = name.split(".")
    return table(table_name, schema=schema)


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            name: conn.execute(select(func.count()).select_from(_table(name))).scalar_one()
            for name, _ in RESET_TABLES
        }


def _state(seeded: Bank) -> dict[str, Any]:
    """What the reset must give back: every demo table's size, every balance, what the
    schedules have done, and the KB versions' statuses."""
    with seeded.owner.connect() as conn:
        balances = dict(conn.execute(select(accounts.c.id, accounts.c.balance_paise)).all())
        done = conn.execute(
            select(func.count()).where(sim_schedule.c.done_at.is_not(None))
        ).scalar_one()
        delivered = conn.execute(
            select(func.count()).where(label_schedule.c.delivered.is_(True))
        ).scalar_one()
        kbs = dict(conn.execute(select(kb_versions.c.id, kb_versions.c.status)).all())
    return {
        "counts": _counts(seeded.owner),
        "balances": balances,
        "schedule_done": done,
        "labels_delivered": delivered,
        "kb": kbs,
    }


def _reset(seeded: Bank, drop: Any = lambda: 0) -> Any:
    registry = JobRegistry()
    register_reset_job(
        registry, sim_engine=seeded.sim, clock=seeded.clock, timeout_s=TIMEOUT_S, drop_sessions=drop
    )
    with seeded.sessions.begin() as s:
        enqueue(s, seeded.clock, JobType.DEMO_RESET, idempotency_key=str(uuid4()))
    job = seeded.queue.claim(WORKER, frozenset({JobType.DEMO_RESET}))
    assert job is not None
    run_job(seeded.queue, registry, job, WORKER)
    with seeded.sessions() as s:
        return s.execute(select(jobs).where(jobs.c.id == job.id)).mappings().one()


def _demo_happens(seeded: Bank, kb: KnowledgeBase) -> None:
    """After the seed: labels are delivered, a seeded schedule row runs, and a new KB version
    goes ACTIVE (S-2 is played by the caller)."""
    now = seeded.clock.now()
    with seeded.sim.begin() as conn:
        assert (
            deliver_labels(conn, until=now, active_hypotheses=frozenset(kb.hypotheses), now=now) > 0
        )
        pending = conn.execute(
            select(sim_schedule.c.id).where(sim_schedule.c.done_at.is_(None)).limit(1)
        ).scalar_one()
        conn.execute(update(sim_schedule).where(sim_schedule.c.id == pending).values(done_at=now))
    with seeded.sessions.begin() as s:
        s.execute(
            update(kb_versions)
            .where(kb_versions.c.status == KbStatus.ACTIVE.value)
            .values(status=KbStatus.RETIRED.value, retired_at=now)
        )
        insert_version(s, seeded.clock, kb, status=KbStatus.ACTIVE, change_summary="after seed")


def test_every_table_is_reset_or_kept(seeded: Bank) -> None:
    """A new table must say whether the demo reset deletes its rows (docs/14 §10)."""
    with seeded.owner.connect() as conn:
        found = {
            f"{schema}.{name}"
            for schema, name in conn.execute(
                select(column("table_schema"), column("table_name"))
                .select_from(table("tables", schema="information_schema"))
                .where(column("table_schema").in_(SCHEMAS), column("table_type") == "BASE TABLE")
            ).all()
        }
    reset = {name for name, _ in RESET_TABLES}
    assert not reset & KEPT_TABLES
    assert found == reset | KEPT_TABLES


def test_the_reset_restores_the_seeded_bank_and_s2_runs_again(
    seeded: Bank, kb: KnowledgeBase
) -> None:
    before = _state(seeded)
    seeded.clock.advance(timedelta(minutes=5))
    guardian, customer = _worker(seeded, kb, story_prior(ARJUN_PRIOR))
    guardian.start()
    try:
        story = _run_story(seeded, guardian, customer, story="S2", mode="autoplay")
        assert story["status"] == JobStatus.SUCCEEDED, story["error"]
        assert _decided(seeded, UUID(story["result"]["session_id"]))["decision"] == "RELEASE"
        _demo_happens(seeded, kb)
        assert _state(seeded) != before

        seeded.clock.advance(timedelta(minutes=1))
        job = _reset(seeded, guardian.abandon)
        assert job["status"] == JobStatus.SUCCEEDED, job["error"]
        result = job["result"]
        assert _state(seeded) == before
        assert result["kb_v1_reactivated"] is True and result["restart_needed"] is True
        assert result["deleted"]["bank.payments"] == 1
        assert result["deleted"]["delphi.guardian_sessions"] == 1
        assert result["deleted"]["bank.devices"] == 1  # Arjun's new phone
        assert result["accounts_restored"] >= 1 and result["schedule_rows_due_again"] == 1
        assert result["labels_undelivered"] > 0
        with seeded.owner.connect() as conn:
            audited = conn.execute(
                select(audit_log.c.actor_type, audit_log.c.payload).where(
                    audit_log.c.action == "DEMO_RESET"
                )
            ).one()
        assert audited.actor_type == "JOB" and audited.payload["deleted"] == result["deleted"]

        seeded.clock.advance(timedelta(minutes=1))
        again = _run_story(seeded, guardian, customer, story="S2", mode="autoplay")
        assert again["status"] == JobStatus.SUCCEEDED, again["error"]
        assert _decided(seeded, UUID(again["result"]["session_id"]))["decision"] == "RELEASE"
    finally:
        guardian.stop()


def test_the_reset_drops_the_worker_sessions_it_deletes(seeded: Bank, kb: KnowledgeBase) -> None:
    """A session still waiting for its simulated customer is dropped before the delete, so no
    task is left driving a deleted session."""
    seeded.clock.advance(timedelta(minutes=5))

    async def never(_seconds: float) -> None:  # the customer never answers
        await __import__("asyncio").Event().wait()

    guardian, customer = _worker(seeded, kb, story_prior(ARJUN_PRIOR))
    guardian.channel._wait = never  # type: ignore[assignment]
    guardian.start()
    try:
        story = _run_story(seeded, guardian, customer, story="S2", mode="autoplay")
        session_id = UUID(story["result"]["session_id"])
        job = _reset(seeded, guardian.abandon)
        assert job["status"] == JobStatus.SUCCEEDED, job["error"]
        assert job["result"]["worker_sessions_dropped"] == 1
        assert guardian.runner.task(session_id) is None
    finally:
        guardian.stop()


def test_a_second_reset_at_once_skips(seeded: Bank) -> None:
    with seeded.owner.connect() as holder, holder.begin():
        holder.execute(select(func.pg_advisory_xact_lock(DEMO_RESET_LOCK_KEY)))
        job = _reset(seeded)
    assert job["status"] == JobStatus.SUCCEEDED
    assert job["result"] == {"skipped": "another demo reset is running"}


def test_children_are_deleted_before_their_parents(seeded: Bank) -> None:
    """Each foreign key between reset tables points from a table deleted earlier to one
    deleted later, unless it is deferrable or cascades (docs/17)."""
    order = {name: i for i, (name, _) in enumerate(RESET_TABLES)}
    with seeded.owner.connect() as conn:
        keys = conn.execute(
            text(
                "SELECT conrelid::regclass::text, confrelid::regclass::text,"
                " condeferrable, confdeltype::text FROM pg_constraint WHERE contype = 'f'"
            )
        ).all()
    wrong = [
        (child, parent)
        for child, parent, deferrable, on_delete in keys
        if child in order
        and parent in order
        and child != parent
        and not deferrable
        and on_delete != "c"
        and order[child] > order[parent]
    ]
    assert wrong == []


def test_a_payment_racing_the_reset_waits_and_is_reset_too(seeded: Bank) -> None:
    """A write in flight when the reset starts (a payment executing: its ledger entry and the
    balance it moves) holds the reset until it commits, and the reset then undoes it as well,
    so the balance and the ledger never disagree."""
    before = _state(seeded)
    seeded.clock.advance(timedelta(minutes=5))
    with seeded.owner.connect() as conn:
        account, payment = conn.execute(
            select(ledger_entries.c.account_id, ledger_entries.c.payment_id).limit(1)
        ).one()
    writer = seeded.owner.connect()
    tx = writer.begin()
    writer.execute(
        update(accounts)
        .where(accounts.c.id == account)
        .values(balance_paise=accounts.c.balance_paise - 100)
    )
    writer.execute(
        ledger_entries.insert().values(
            account_id=account,
            payment_id=payment,
            delta_paise=-100,
            balance_after_paise=0,
            at=seeded.clock.now(),
        )
    )
    done: list[Any] = []
    reset = threading.Thread(target=lambda: done.append(_reset(seeded)))
    reset.start()
    try:
        reset.join(timeout=2)
        assert reset.is_alive()  # waiting on the writer's tables
        tx.commit()
    finally:
        writer.close()
        reset.join(timeout=TIMEOUT_S)
    job = done[0]
    assert job["status"] == JobStatus.SUCCEEDED, job["error"]
    assert job["result"]["deleted"]["bank.ledger_entries"] == 1
    assert _state(seeded) == before


# ---------------------------------------------------------------- the endpoint


class FakeRunner:
    async def resume_all(self) -> list[UUID]:
        return []

    async def shutdown(self) -> int:
        return 0


class FakeLive:
    """The api's runner slot, counting how often the endpoint drops its sessions."""

    def __init__(self) -> None:
        self.abandoned = 0
        self.runner = FakeRunner()

    async def abandon(self) -> int:
        self.abandoned += 1
        return 0


def test_the_endpoint_is_admin_only_and_needs_the_simulation(seeded: Bank) -> None:
    with _client(seeded, Role.INVESTIGATOR, simulation_enabled=True) as client:
        assert client.post(RESET, headers=_key()).status_code == 403
    with _client(seeded, Role.ADMIN) as client:  # DELPHI_SIMULATION_ENABLED off
        off = client.post(RESET, headers=_key())
        assert off.status_code == 503 and off.json()["code"] == "NOT_INITIALISED"
        assert client.post(RESET).status_code == 422  # no Idempotency-Key


def test_the_endpoint_queues_one_reset_and_drops_the_live_sessions(seeded: Bank) -> None:
    live = FakeLive()
    with _client(seeded, Role.ADMIN, live=live, simulation_enabled=True) as client:
        key = _key()
        queued = client.post(RESET, headers=key)
        assert queued.status_code == 202, queued.text
        job = queued.json()
        assert (job["type"], job["status"]) == ("DEMO_RESET", "QUEUED")
        assert live.abandoned == 1
        replay = client.post(RESET, headers=key)
        assert replay.json()["id"] == job["id"] and live.abandoned == 1
        busy = client.post(RESET, headers=_key())
        assert busy.status_code == 409 and busy.json()["code"] == "JOB_ALREADY_RUNNING"


def test_the_endpoint_needs_a_dataset(seeded: Bank) -> None:
    with seeded.owner.begin() as conn:  # an empty bank: no dataset yet
        conn.execute(text("DELETE FROM sim_truth.persona_manifests"))
        conn.execute(text("DELETE FROM delphi.datasets"))
    with _client(seeded, Role.ADMIN, live=FakeLive(), simulation_enabled=True) as client:
        refused = client.post(RESET, headers=_key())
    assert refused.status_code == 409 and refused.json()["code"] == "INVALID_STATE_TRANSITION"


def test_payments_and_stories_are_refused_while_a_reset_is_pending(
    seeded: Bank, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing new starts between the reset's request and its job: it would race the deletes."""
    monkeypatch.setattr(demo_router, "require_initialised", lambda _request: None)
    with seeded.sessions.begin() as s:
        enqueue(s, seeded.clock, JobType.DEMO_RESET, idempotency_key=str(uuid4()))
    payment = {
        "payer_account_id": str(uuid4()),
        "payee_account_id": str(uuid4()),
        "amount_paise": 100,
        "channel": "UPI_PUSH",
        "device_id": str(uuid4()),
    }
    with _client(seeded, Role.ADMIN, live=FakeLive(), simulation_enabled=True) as client:
        story = client.post(f"{STORIES}/S2/run", json={"mode": "autoplay"}, headers=_key())
        paid = client.post(SESSIONS, json={"payment": payment}, headers=_key())
    for refused in (story, paid):
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "INVALID_STATE_TRANSITION"
        assert "demo reset is in progress" in refused.json()["detail"]
