"""SQLAlchemy Core descriptions of the platform tables (docs/17 §3).

Migration 0001 creates these tables; this module only describes them so platform code can build
parameterised queries without raw SQL (docs/24 §4). Keep the two in step: an integration test
compares these columns with the migrated database.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

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
)
