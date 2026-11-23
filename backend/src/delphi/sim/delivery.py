"""Label delivery: the bank learns a label when it falls due (docs/14 §7–8, #159).

The simulator schedules every label it will ever deliver in ``sim_truth.label_schedule``, which
the app role cannot read. ``deliver_labels`` copies the rows due by ``until`` into
``delphi.labels`` (``delphi.platform.labels``), with ``known_at = due_at``, and marks them
delivered. A complaint whose ground-truth key is not in the ACTIVE KB at delivery becomes
``FRAUD_OTHER`` (``observed_hypothesis``). It runs as ``delphi_sim``, under one advisory lock, and
is idempotent: a delivered row is never delivered again, and ``delphi.labels`` keeps one label
per (subject, source).

It works in bounded batches (the 2026-11-22 incident: one transaction delivering everything
with 5,000-tuple ``IN`` lists ran Postgres out of memory). A batch is one ``UPDATE … WHERE key IN
(SELECT … ORDER BY due_at LIMIT n FOR UPDATE SKIP LOCKED) RETURNING …`` on the partial
``due_at`` index, then the insert of those labels. The recurring job commits each batch on its
own, under ``statement_timeout`` and a small ``work_mem``, skips when another run holds the
lock, and stops after ``BATCHES_PER_RUN`` batches for the next run to carry on.

Two callers: ``GENERATE_DATASET`` delivers the labels due by the end of the history in the same
transaction that writes the dataset, so a freshly seeded bank already knows its history's labels;
the recurring ``LABEL_DELIVERY`` job delivers what falls due by the clock afterwards. On `test`
and `eval` the history ends at a fixed date in the past, so the first ``LABEL_DELIVERY`` delivers
every remaining label at once. That is harmless: the trainers keep only labels known by their
training cutoff (docs/13 §4).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select, text, tuple_, update

from delphi.platform.labels import LabelRow, record_labels
from delphi.platform.locks import LABEL_DELIVERY_LOCK_KEY
from delphi.sim.labels import (
    FRAUD_OTHER,
    LabelSource,
    ScheduledLabel,
    SubjectType,
    observed_hypothesis,
)
from delphi.sim.truth import label_schedule

# Schedule rows taken and marked per statement. Each batch is one UPDATE whose rows are chosen by
# a LIMIT subquery on the partial due_at index, so no statement carries a list of keys: a
# 5,000-tuple `IN (...)` list was planned as a 5,000-arm OR and ran Postgres out of memory.
DELIVERY_BATCH = 1_000
# Batches per LABEL_DELIVERY run, one transaction each; the next run (60 s later) carries on.
BATCHES_PER_RUN = 50
# Limits for the job's transactions (docs/23: postgres runs in 1 GiB).
STATEMENT_TIMEOUT = "30s"
WORK_MEM = "16MB"


@dataclass(frozen=True, slots=True)
class Batch:
    marked: int  # schedule rows marked delivered
    recorded: int  # labels the bank did not have yet


def deliver_batch(
    conn: Connection,
    *,
    until: datetime,
    active_hypotheses: frozenset[str],
    now: datetime,
    limit: int = DELIVERY_BATCH,
) -> Batch:
    """Mark up to ``limit`` undelivered labels due by ``until`` as delivered, oldest first, and
    record them in ``delphi.labels``, in the caller's transaction. The caller holds the delivery
    lock. ``marked == 0`` means nothing more is due."""
    ls = label_schedule
    key = tuple_(ls.c.subject_type, ls.c.subject_id, ls.c.label_source)
    picked = (
        select(ls.c.subject_type, ls.c.subject_id, ls.c.label_source)
        .where(~ls.c.delivered, ls.c.due_at <= until)
        .order_by(ls.c.due_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    due = conn.execute(
        update(ls)
        .where(key.in_(picked))
        .values(delivered=True)
        .returning(
            ls.c.subject_type, ls.c.subject_id, ls.c.hypothesis_key, ls.c.label_source, ls.c.due_at
        )
    ).all()
    if not due:
        return Batch(0, 0)
    return Batch(len(due), record_labels(conn, _observed(due, active_hypotheses), now=now))


def lock_delivery(conn: Connection, *, wait: bool) -> bool:
    """Take the delivery lock for this transaction. Without ``wait``, False if another
    transaction holds it (a LABEL_DELIVERY run is already delivering)."""
    if wait:
        conn.execute(select(func.pg_advisory_xact_lock(LABEL_DELIVERY_LOCK_KEY)))
        return True
    return bool(
        conn.execute(select(func.pg_try_advisory_xact_lock(LABEL_DELIVERY_LOCK_KEY))).scalar_one()
    )


def limit_transaction(conn: Connection) -> None:
    """Bound one delivery transaction: a statement timeout and a small work_mem."""
    conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    conn.execute(text(f"SET LOCAL work_mem = '{WORK_MEM}'"))


def deliver_labels(
    conn: Connection, *, until: datetime, active_hypotheses: frozenset[str], now: datetime
) -> int:
    """Deliver every undelivered label due at or before ``until``, in the caller's transaction
    (``GENERATE_DATASET``: the seeded bank knows its history's labels at once). Returns how many
    were recorded. ``conn`` connects as ``delphi_sim``. Each batch is bounded; the recurring job
    uses ``deliver_batch`` with a transaction per batch instead."""
    lock_delivery(conn, wait=True)
    recorded = 0
    while True:
        batch = deliver_batch(conn, until=until, active_hypotheses=active_hypotheses, now=now)
        if batch.marked == 0:
            return recorded
        recorded += batch.recorded


def stored_key(label: ScheduledLabel, active_hypotheses: frozenset[str]) -> str:
    """docs/05 §8: every stored key is an ACTIVE-KB hypothesis or FRAUD_OTHER. A complaint
    names its hypothesis only if the KB has it (``observed_hypothesis``, docs/14 §7); any other
    source with a key outside the KB is stored as FRAUD_OTHER too, so nothing leaks a hidden
    typology."""
    key = observed_hypothesis(label, active_hypotheses)
    return key if key in active_hypotheses else FRAUD_OTHER


def _observed(rows: Sequence[Any], active_hypotheses: frozenset[str]) -> Iterator[LabelRow]:
    for r in rows:
        scheduled = ScheduledLabel(
            SubjectType(r.subject_type),
            str(r.subject_id),
            r.hypothesis_key,
            LabelSource(r.label_source),
            r.due_at,
        )
        yield LabelRow(
            subject_type=r.subject_type,
            subject_id=r.subject_id,
            hypothesis_key=stored_key(scheduled, active_hypotheses),
            label_source=r.label_source,
            known_at=r.due_at,
        )
