"""SQLAlchemy Core descriptions of ``delphi.cases`` (migration 0012),
``delphi.evidence_records`` (0013), ``delphi.police_alert_drafts`` (0014),
``delphi.trace_events`` (0015) and ``delphi.review_sweep_failures`` (0020, docs/17). The
migrations are the source of truth for constraints; a test compares these columns with the
migrated schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="delphi")


def _ts(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


cases = Table(
    "cases",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("type", Text, nullable=False),
    Column("protocol", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("subject_payment_id", UUID(as_uuid=True)),
    Column("subject_account_id", UUID(as_uuid=True)),
    Column("guardian_session_id", UUID(as_uuid=True)),
    Column("kb_version_id", Integer, nullable=False),
    Column("prior_components", JSONB, nullable=False),
    Column("posterior", JSONB, nullable=False),
    Column("recommended_decision", Text),
    Column("risk_by_decision", JSONB),
    Column("leading_hypothesis", Text, nullable=False),
    Column("campaign_id", UUID(as_uuid=True)),
    Column("amount_at_stake_paise", BigInteger, nullable=False),
    _ts("opened_at"),
    _ts("sla_due_at"),
    Column("disposition", Text),
    Column("disposition_typology", Text),
    Column("disposition_confidence", SmallInteger),
    Column("is_override", Boolean, nullable=False),
    Column("override_rationale", Text),
    Column("disposed_by", UUID(as_uuid=True)),
    _ts("disposed_at", nullable=True),
    Column("dd_pack_complete", Boolean, nullable=False),
    Column("unexplained", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    _ts("created_at"),
)

evidence_records = Table(
    "evidence_records",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("case_id", UUID(as_uuid=True), nullable=False),
    Column("source_key", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("record_refs", JSONB, nullable=False),
    Column("fetch_ms", Integer, nullable=False),
    Column("posterior_before", JSONB, nullable=False),
    Column("posterior_after", JSONB, nullable=False),
    Column("info_yield_bits", Float, nullable=False),
    Column("rank_at_request", Integer),
    Column("net_value_at_request_paise", BigInteger),
    Column("requested_by", UUID(as_uuid=True)),
    _ts("requested_at"),
    Column("narrative", Text),
    Column("narrative_source", Text),
    Column("narrative_citations", JSONB),
    Column("superseded_by", UUID(as_uuid=True)),
    _ts("created_at"),
)

police_alert_drafts = Table(
    "police_alert_drafts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("case_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("acted_by", UUID(as_uuid=True)),
    _ts("acted_at", nullable=True),
    _ts("created_at"),
)

trace_events = Table(
    "trace_events",
    metadata,
    Column("id", BigInteger, primary_key=True),  # GENERATED ALWAYS AS IDENTITY
    Column("case_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True)),
    Column("type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    _ts("at"),
)

review_sweep_failures = Table(
    "review_sweep_failures",
    metadata,
    Column("account_id", UUID(as_uuid=True), primary_key=True),
    Column("attempts", Integer, nullable=False),
    Column("last_error", Text, nullable=False),
    _ts("next_attempt_at"),
    _ts("created_at"),
    _ts("updated_at"),
)
