"""The demo reset, `DEMO_RESET` (docs/14 §10, FR-SIM-08, #101): back to the state right after
the seed, so S-1 can run again.

The seed marks its dataset with ``seed_completed_at``; everything the demo did since then is
deleted, in one transaction, children before parents (``RESET_TABLES``, each by its creation
time), and the rows the demo changed are put back:

- account balances lose the deltas of the ledger entries the reset deletes;
- schedule rows done after the marker are not done again;
- labels delivered after the marker are deleted, and their schedule rows are undelivered, so
  ``LABEL_DELIVERY`` delivers them again against the re-activated KB v1 (#159);
- KB v1 is ACTIVE again, and any other version left is RETIRED.

The platform's own tables (``KEPT_TABLES``: users, jobs, the audit chain, sensing models …) are
not demo data and stay. A test checks every table is in one list or the other.

Sessions still running are dropped first by both runners (the api's when the reset is asked
for, the worker's here), so no task drives a session the reset deletes. The transaction runs
under ``DEMO_RESET_LOCK_KEY`` (a second reset at once skips) and ``LABEL_DELIVERY_LOCK_KEY`` (no
delivery interleaves), with a statement timeout (``demo_reset_timeout_s``). The deletes are
set-based (`DELETE … WHERE created_at > :marker`): no rows or key lists pass through Python.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, column, func, select, table, text, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.store import KbStatus, kb_versions
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext, JobRegistry, JobType
from delphi.platform.locks import DEMO_RESET_LOCK_KEY, LABEL_DELIVERY_LOCK_KEY
from delphi.sim.bank import accounts, ledger_entries, sim_schedule
from delphi.sim.catalog import datasets
from delphi.sim.truth import label_schedule

KB_V1_ID = 1  # `delphi migrate` inserts KB v1 first (docs/07 §1)
_MS_PER_S = 1000

# The demo's rows, deleted children first: (schema.table, its creation-time column).
RESET_TABLES: tuple[tuple[str, str], ...] = (
    ("delphi.police_alert_drafts", "created_at"),
    ("delphi.trace_events", "at"),
    ("delphi.evidence_records", "created_at"),
    ("delphi.cases", "created_at"),  # its sessions point back at it: DEFERRABLE (docs/17)
    ("delphi.guardian_sessions", "started_at"),  # probe_runs go with them (ON DELETE CASCADE)
    ("delphi.labels", "created_at"),
    ("delphi.kb_versions", "created_at"),
    ("sim_truth.payment_truth", "created_at"),
    ("sim_truth.scenario_instances", "created_at"),
    ("sim_truth.account_truth", "created_at"),
    ("sim_truth.schedule_truth", "created_at"),
    ("sim_truth.label_schedule", "created_at"),
    ("bank.session_telemetry", "created_at"),
    ("bank.ledger_entries", "at"),
    ("bank.login_events", "at"),
    ("bank.payments", "created_at"),
    ("bank.sim_schedule", "created_at"),
    ("bank.payees", "created_at"),
    ("bank.complaints", "created_at"),
    ("bank.crm_events", "at"),
    ("bank.sim_registry_listings", "created_at"),
    ("bank.devices", "created_at"),
    ("bank.accounts", "created_at"),  # account_scores go with them (ON DELETE CASCADE)
    ("bank.customers", "created_at"),
    ("bank.sim_phone_risk", "created_at"),
)
# Not demo data: the platform's records, the dataset and what was trained on it, and the
# frozen response models. `probe_runs` goes with its session.
KEPT_TABLES: frozenset[str] = frozenset(
    {
        "bank.account_scores",  # the mule scorer's, refreshed by MULE_SWEEP
        "delphi.audit_log",  # append-only; the reset is audited in it
        "delphi.auth_sessions",
        "delphi.datasets",
        "delphi.idempotency_keys",
        "delphi.jobs",
        "delphi.llm_calls",
        "delphi.model_artifacts",
        "delphi.outbox_events",
        "delphi.probe_runs",
        "delphi.users",
        "sim_truth.persona_manifests",
        "sim_truth.response_overrides",
    }
)


@dataclass(frozen=True, slots=True)
class Marker:
    dataset_id: Any
    seed_completed_at: datetime
    history_end: datetime | None


def _marker(session: Session) -> Marker:
    d = datasets.c
    row = session.execute(
        select(d.id, d.seed_completed_at, d.stats["history_end"].astext).limit(1)
    ).first()
    if row is None or row.seed_completed_at is None:
        raise LookupError("the bank holds no seeded dataset: nothing to reset to")
    ended = datetime.fromisoformat(row[2]) if row[2] else None
    return Marker(row.id, row.seed_completed_at, ended)


def _restore_balances(session: Session, marker: datetime) -> int:
    """Take the deltas of the ledger entries after the marker back off their accounts."""
    le = ledger_entries.c
    moved = (
        select(le.account_id, func.sum(le.delta_paise).label("delta"))
        .where(le.at > marker)
        .group_by(le.account_id)
        .subquery()
    )
    done = session.execute(
        update(accounts)
        .where(accounts.c.id == moved.c.account_id)
        .values(
            balance_paise=accounts.c.balance_paise - moved.c.delta,
            version=accounts.c.version + 1,
        )
    )
    return int(done.rowcount)  # type: ignore[attr-defined]


def _undo_schedule(session: Session, marker: datetime) -> int:
    """Seeded schedule rows done after the marker are due again, as they were after the seed."""
    s = sim_schedule.c
    done = session.execute(
        update(sim_schedule).where(s.created_at <= marker, s.done_at > marker).values(done_at=None)
    )
    return int(done.rowcount)  # type: ignore[attr-defined]


def _undeliver_labels(session: Session, marker: Marker) -> int:
    """The seed delivered the labels due by the history's end (at the marker); any delivered
    since are delivered again by LABEL_DELIVERY."""
    cut = marker.history_end or marker.seed_completed_at
    ls = label_schedule.c
    done = session.execute(
        update(label_schedule)
        .where(ls.delivered.is_(True), ls.due_at > cut, ls.created_at <= marker.seed_completed_at)
        .values(delivered=False)
    )
    return int(done.rowcount)  # type: ignore[attr-defined]


def _delete_after(session: Session, marker: datetime) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for name, created in RESET_TABLES:
        schema, table_name = name.split(".")
        t = table(table_name, column(created), schema=schema)
        done = session.execute(t.delete().where(t.c[created] > marker))
        deleted[name] = int(done.rowcount)  # type: ignore[attr-defined]
    return deleted


def _reactivate_kb_v1(session: Session, now: datetime) -> bool:
    """KB v1 ACTIVE again; any other version the reset kept is RETIRED. True if it changed."""
    k = kb_versions.c
    status = session.execute(select(k.status).where(k.id == KB_V1_ID)).scalar_one_or_none()
    if status is None:
        raise LookupError("KB v1 is missing: run `delphi migrate`")
    live = (KbStatus.ACTIVE.value, KbStatus.SHADOW.value)
    session.execute(
        update(kb_versions)
        .where(k.id != KB_V1_ID, k.status.in_(live))
        .values(status=KbStatus.RETIRED.value, retired_at=now, version=k.version + 1)
    )
    if status == KbStatus.ACTIVE.value:
        return False
    session.execute(
        update(kb_versions)
        .where(k.id == KB_V1_ID)
        .values(
            status=KbStatus.ACTIVE.value,
            activated_at=now,
            retired_at=None,
            version=k.version + 1,
        )
    )
    return True


@dataclass(frozen=True, slots=True)
class DemoResetJob:
    sessions: sessionmaker[Session]  # as delphi_sim: the reset deletes ground truth too
    clock: Clock
    timeout_s: float
    drop_sessions: Callable[[], int]  # the worker's runner drops the sessions it drives

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        dropped = self.drop_sessions()
        now = self.clock.now()
        with self.sessions.begin() as session:
            got = session.execute(select(func.pg_try_advisory_xact_lock(DEMO_RESET_LOCK_KEY)))
            if not got.scalar_one():
                return {"skipped": "another demo reset is running"}
            session.execute(
                select(
                    func.set_config("statement_timeout", str(int(self.timeout_s * _MS_PER_S)), True)
                )
            )
            session.execute(select(func.pg_advisory_xact_lock(LABEL_DELIVERY_LOCK_KEY)))
            session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            marker = _marker(session)
            at = marker.seed_completed_at
            balances = _restore_balances(session, at)  # before their ledger entries go
            schedule = _undo_schedule(session, at)
            undelivered = _undeliver_labels(session, marker)
            deleted = _delete_after(session, at)
            kb_changed = _reactivate_kb_v1(session, now)
            result = {
                "seed_completed_at": at.isoformat(),
                "deleted": deleted,
                "accounts_restored": balances,
                "schedule_rows_due_again": schedule,
                "labels_undelivered": undelivered,
                "worker_sessions_dropped": dropped,
                "kb_v1_reactivated": kb_changed,
                # The api and the worker load the ACTIVE KB at start-up (TD: no hot reload).
                "restart_needed": kb_changed,
            }
            record(
                session,
                self.clock,
                actor_type=ActorType.JOB,
                actor_id=str(ctx.job.id),
                action="DEMO_RESET",
                entity_type="dataset",
                entity_id=str(marker.dataset_id),
                payload=result,
            )
        return result


def register_reset_job(
    registry: JobRegistry,
    *,
    sim_engine: Engine,
    clock: Clock,
    timeout_s: float,
    drop_sessions: Callable[[], int],
) -> None:
    """Add the `DEMO_RESET` handler. The worker wires ``drop_sessions`` to its Guardian
    runner."""
    registry.register(
        JobType.DEMO_RESET,
        DemoResetJob(
            sessions=sessionmaker(sim_engine, expire_on_commit=False),
            clock=clock,
            timeout_s=timeout_s,
            drop_sessions=drop_sessions,
        ),
    )
