"""SQLAlchemy Core descriptions of the Guardian tables (migration 0010, docs/17 §3). The migration
is the source of truth for constraints; a test compares these columns with the migrated schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="delphi")


def _ts(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


guardian_sessions = Table(
    "guardian_sessions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("payment_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("decision", Text),
    Column("terminal_reason", Text),
    Column("kb_version_id", Integer, nullable=False),
    Column("prior", JSONB),
    Column("p_model", JSONB),
    Column("anomaly_percentile", Float),
    Column("open_mass", Float),
    Column("open_mass_shares", JSONB),
    Column("top_features", JSONB),
    Column("posterior", JSONB),
    Column("risk_by_decision", JSONB),
    Column("unexplained", Boolean, nullable=False),
    Column("mode", Text, nullable=False),
    Column("runner", Text, nullable=False),
    Column("rng_seed", BigInteger, nullable=False),
    _ts("started_at"),
    _ts("decided_at", nullable=True),
    Column("case_id", UUID(as_uuid=True)),
    Column("baseline_decisions", JSONB),
    Column("dd_pack_complete", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
)

probe_runs = Table(
    "probe_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("session_id", UUID(as_uuid=True), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("probe_key", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("selected_by", Text),
    Column("propensity", Float),
    Column("eligible_set", JSONB),
    Column("candidates", JSONB),
    Column("lookahead_depth", Integer),
    Column("voi_myopic_paise", BigInteger),
    Column("net_value_paise", BigInteger),
    Column("cost_paise", BigInteger),
    Column("variant_id", Text),
    Column("language", Text),
    Column("raw_response", JSONB),
    Column("outcome", Text),
    Column("posterior_before", JSONB),
    Column("posterior_after", JSONB),
    Column("info_yield_bits", Float),
    _ts("started_at"),
    _ts("completed_at", nullable=True),
)
