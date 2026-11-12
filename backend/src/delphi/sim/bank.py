"""SQLAlchemy Core definitions of the simulated bank's tables (docs/17 §3, migration 0002).

The migration is the source of truth for constraints and indexes; these definitions carry the
columns and keys the generator writes through. A test compares them with the migrated schema.
"""

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
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, JSONB

metadata = MetaData(schema="bank")


def _ts(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


customers = Table(
    "customers",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("age", SmallInteger, nullable=False),
    Column("language", Text, nullable=False),
    Column("is_vulnerable", Boolean, nullable=False),
    Column("disability_flag", Boolean, nullable=False),
    Column("segment", Text, nullable=False),
    Column("income_band", Text, nullable=False),
    Column("consent_call_status", Boolean, nullable=False),
    Column("home_city", Text, nullable=False),
    Column("persona_key", Text),
    _ts("created_at"),
)

accounts = Table(
    "accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid),
    Column("bank_code", Text, nullable=False),
    Column("is_merchant", Boolean, nullable=False),
    Column("merchant_category", Text),
    _ts("opened_at"),
    Column("kyc_level", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("balance_paise", BigInteger, nullable=False),
    Column("phone_number", Text),
    Column("vpa", Text),
    _ts("created_at"),
    Column("version", Integer, nullable=False),
)

devices = Table(
    "devices",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("os", Text, nullable=False),
    _ts("first_seen_at"),
    _ts("created_at"),
)

payees = Table(
    "payees",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("label", Text, nullable=False),
    _ts("added_at"),
    _ts("created_at"),
)

sim_schedule = Table(
    "sim_schedule",
    metadata,
    Column("id", Uuid, primary_key=True),
    _ts("due_at"),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    _ts("done_at", nullable=True),
    _ts("created_at"),
)

payments = Table(
    "payments",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("payer_account_id", Uuid, nullable=False),
    Column("payee_account_id", Uuid, nullable=False),
    Column("amount_paise", BigInteger, nullable=False),
    Column("channel", Text, nullable=False),
    _ts("initiated_at"),
    Column("device_id", Uuid),
    Column("status", Text, nullable=False),
    Column("origin", Text, nullable=False),
    _ts("executed_at", nullable=True),
    Column("scheduled_by", Uuid),
    _ts("created_at"),
)

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("payment_id", Uuid, nullable=False),
    Column("delta_paise", BigInteger, nullable=False),
    Column("balance_after_paise", BigInteger, nullable=False),
    _ts("at"),
)

session_telemetry = Table(
    "session_telemetry",
    metadata,
    Column("payment_id", Uuid, primary_key=True),
    Column("typing_cadence_z", Float, nullable=False),
    Column("paste_on_account_field", Boolean, nullable=False),
    Column("max_field_dwell_s", Float, nullable=False),
    Column("remote_access_active", Boolean, nullable=False),
    Column("screen_share_active", Boolean, nullable=False),
    Column("on_call", Boolean, nullable=False),
    Column("call_counterparty_known", Boolean),
    Column("session_duration_s", Float, nullable=False),
    _ts("created_at"),
)

login_events = Table(
    "login_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid, nullable=False),
    Column("device_id", Uuid, nullable=False),
    Column("ip_prefix24", CIDR, nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("city", Text, nullable=False),
    _ts("at"),
)

crm_events = Table(
    "crm_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid, nullable=False),
    Column("type", Text, nullable=False),
    _ts("at"),
)

complaints = Table(
    "complaints",
    metadata,
    Column("id", Uuid, primary_key=True),
    _ts("reported_at"),
    Column("victim_customer_id", Uuid, nullable=False),
    Column("channel", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("payment_ids", ARRAY(Uuid), nullable=False),
    Column("counterparty_account_ids", ARRAY(Uuid), nullable=False),
    _ts("created_at"),
)

sim_registry_listings = Table(
    "sim_registry_listings",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    _ts("listed_at"),
    _ts("created_at"),
)

sim_phone_risk = Table(
    "sim_phone_risk",
    metadata,
    Column("phone_number", Text, primary_key=True),
    Column("category", Text, nullable=False),
    _ts("created_at"),
)

# Created by migration 0007 with `delphi.model_artifacts`, which its foreign key names (TD-7).
# The mule scorer writes it (MULE_SWEEP, docs/13 §6); the generator never does.
account_scores = Table(
    "account_scores",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    Column("p_mule", Float, nullable=False),
    _ts("computed_at"),
    Column("model_artifact_id", Uuid, nullable=False),
)
