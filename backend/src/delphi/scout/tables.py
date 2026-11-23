"""SQLAlchemy Core definitions of the Scout tables (migration 0016, docs/17)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData(schema="delphi")


def _ts(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


scout_runs = Table(
    "scout_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("job_id", UUID(as_uuid=True)),
    _ts("started_at"),
    _ts("finished_at", nullable=True),
    Column("n_items", Integer, nullable=False),
    Column("n_clusters", Integer, nullable=False),
    Column("params", JSONB, nullable=False),
)

scout_clusters = Table(
    "scout_clusters",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("size", Integer, nullable=False),
    Column("member_refs", JSONB, nullable=False),
    Column("distinguishing_features", JSONB, nullable=False),
    Column("outcome_profile", JSONB, nullable=False),
    _ts("created_at"),
)

typology_drafts = Table(
    "typology_drafts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("cluster_id", UUID(as_uuid=True)),
    Column("status", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("spec", JSONB, nullable=False),
    Column("last_edited_by", UUID(as_uuid=True)),
    Column("editors", ARRAY(UUID(as_uuid=True)), nullable=False),
    Column("backtest_report", JSONB),
    Column("backtested_revision", Integer),
    Column("shadow_kb_version_id", Integer),
    Column("approved_by", UUID(as_uuid=True)),
    Column("rejected_reason", Text),
    _ts("created_at"),
    _ts("updated_at"),
    Column("version", Integer, nullable=False),
)

scout_queue_dismissals = Table(
    "scout_queue_dismissals",
    metadata,
    Column("subject_type", Text, primary_key=True),
    Column("subject_id", UUID(as_uuid=True), primary_key=True),
    Column("reason", Text, nullable=False),
    Column("dismissed_by", UUID(as_uuid=True), nullable=False),
    _ts("dismissed_at"),
)
