"""SQLAlchemy Core descriptions of the platform tables (docs/17 §3).

Migration 0001 creates these tables; this module only describes them so platform code can build
parameterised queries without raw SQL (docs/24 §4). Keep the two in step: an integration test
compares these columns with the migrated database.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

metadata = MetaData(schema="delphi")

jobs = Table(
    "jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    Column("result", JSONB),
    Column("error", Text),
    Column("progress", Float, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("locked_by", Text),
    Column("locked_until", DateTime(timezone=True)),
    Column("run_after", DateTime(timezone=True), nullable=False),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
)

outbox_events = Table(
    "outbox_events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("type", Text, nullable=False),
    Column("entity_id", Text),
    Column("payload", JSONB, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("endpoint", Text, nullable=False),
    Column("request_sha256", LargeBinary, nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("id", LargeBinary, primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("ip", INET),
    Column("user_agent", Text),
)

users = Table(
    "users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("username", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("disabled", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("version", Integer, nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text),
    Column("action", Text, nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Text),
    Column("payload", JSONB, nullable=False),
    Column("prev_hash", LargeBinary, nullable=False),
    Column("hash", LargeBinary, nullable=False),
)

# Migration 0008 (#104). Platform-owned like the audit log, so RETENTION_SWEEP can null the
# bodies after 30 days without importing `delphi.llm`.
llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text),
    Column("prompt_version", Text, nullable=False),
    Column("request_redacted", JSONB),
    Column("response", JSONB),
    Column("status", Text, nullable=False),
    Column("validation", JSONB, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
