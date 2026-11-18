"""The labels the bank has observed (``delphi.labels``, docs/17, docs/05 §8, #159).

A label says what a payment or an account turned out to be, from which source, and since when
the bank knows it. Several features write labels: the simulator's ``LABEL_DELIVERY`` (complaints,
the no-complaint maturity, registry listings) and, from M5, the Navigator (outreach and
dispositions). So the table lives here, with no business logic: callers decide the key and the
source. Writes are idempotent per (subject, source); the first label of a source wins.

Readers use ``label_precedence`` (docs/05 §8) to choose between a subject's labels; the sensing
trainers do that in ``delphi.sensing.prior.observed_labels``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Column, Connection, DateTime, MetaData, Table, Text, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert

from delphi.platform.ids import uuid7

metadata = MetaData(schema="delphi")

labels = Table(
    "labels",
    metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", PgUUID(as_uuid=True), nullable=False),
    Column("hypothesis_key", Text, nullable=False),
    Column("label_source", Text, nullable=False),
    Column("known_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Rows per INSERT statement when recording many labels (a seed delivers the whole history's).
BATCH = 5_000


@dataclass(frozen=True, slots=True)
class LabelRow:
    subject_type: str  # PAYMENT | ACCOUNT
    subject_id: UUID
    hypothesis_key: str
    label_source: str
    known_at: datetime


def record_labels(conn: Connection, rows: Iterable[LabelRow], *, now: datetime) -> int:
    """Insert ``rows``, skipping any (subject, source) the bank already has. Returns how many
    were new. ``now`` seeds the UUIDv7 ids (docs/17 §1: ids come from the application)."""
    created = 0
    batch: list[dict[str, object]] = []
    stamp = int(now.timestamp() * 1000)

    def flush() -> int:
        if not batch:
            return 0
        statement = (
            insert(labels)
            .values(batch)
            .on_conflict_do_nothing(constraint="labels_subject_source_key")
            .returning(labels.c.id)
        )
        new = len(conn.execute(statement).all())
        batch.clear()
        return new

    for row in rows:
        batch.append(
            {
                "id": uuid7(unix_ms=stamp),
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "hypothesis_key": row.hypothesis_key,
                "label_source": row.label_source,
                "known_at": row.known_at,
            }
        )
        if len(batch) >= BATCH:
            created += flush()
    return created + flush()
