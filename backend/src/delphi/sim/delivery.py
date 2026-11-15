"""Label delivery: the bank learns a label when it falls due (docs/14 §7–8, #159).

The simulator schedules every label it will ever deliver in ``sim_truth.label_schedule``, which
the app role cannot read. ``deliver_labels`` copies the rows due by ``until`` into
``delphi.labels`` (``delphi.platform.labels``), with ``known_at = due_at``, and marks them
delivered. A complaint whose ground-truth key is not in the ACTIVE KB at delivery becomes
``FRAUD_OTHER`` (``observed_hypothesis``). It runs as ``delphi_sim``, under one advisory lock, and
is idempotent: a delivered row is never delivered again, and ``delphi.labels`` keeps one label
per (subject, source).

Two callers: ``GENERATE_DATASET`` delivers the labels due by the end of the history in the same
transaction that writes the dataset, so a freshly seeded bank already knows its history's labels;
the recurring ``LABEL_DELIVERY`` job delivers what falls due by the clock afterwards. On `test`
and `eval` the history ends at a fixed date in the past, so the first ``LABEL_DELIVERY`` delivers
every remaining label at once. That is harmless: the trainers keep only labels known by their
training cutoff (docs/13 §4).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select, tuple_, update

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

# Schedule rows read and marked per round.
DELIVERY_BATCH = 5_000


def deliver_labels(
    conn: Connection, *, until: datetime, active_hypotheses: frozenset[str], now: datetime
) -> int:
    """Deliver every undelivered label due at or before ``until``. Returns how many were
    recorded. Runs in the caller's transaction (``conn`` connects as ``delphi_sim``)."""
    conn.execute(select(func.pg_advisory_xact_lock(LABEL_DELIVERY_LOCK_KEY)))
    ls = label_schedule
    delivered = 0
    while True:
        due = conn.execute(
            select(
                ls.c.subject_type,
                ls.c.subject_id,
                ls.c.hypothesis_key,
                ls.c.label_source,
                ls.c.due_at,
            )
            .where(~ls.c.delivered, ls.c.due_at <= until)
            .order_by(ls.c.due_at, ls.c.subject_type, ls.c.subject_id, ls.c.label_source)
            .limit(DELIVERY_BATCH)
        ).all()
        if not due:
            return delivered
        delivered += record_labels(conn, _observed(due, active_hypotheses), now=now)
        keys = [(r.subject_type, r.subject_id, r.label_source) for r in due]
        conn.execute(
            update(ls)
            .where(tuple_(ls.c.subject_type, ls.c.subject_id, ls.c.label_source).in_(keys))
            .values(delivered=True)
        )


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
