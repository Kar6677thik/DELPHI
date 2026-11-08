"""SQLAlchemy Core definitions of the ground-truth tables (docs/17 §3, migrations 0002, 0004,
0005).

Only ``delphi.sim`` and ``delphi.evaluation`` may import this module (docs/04 §4.1 rule 5, an
import-linter contract); at the database, only the ``delphi_sim`` role can read the
``sim_truth`` schema (docs/17 §2).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(schema="sim_truth")


def _ts(name: str) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=False)


scenario_instances = Table(
    "scenario_instances",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("scenario_type", Text, nullable=False),
    Column("hypothesis_key", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    _ts("started_at"),
    _ts("created_at"),
)

payment_truth = Table(
    "payment_truth",
    metadata,
    Column("payment_id", Uuid, primary_key=True),
    Column("scenario_instance_id", Uuid, nullable=False),
    Column("is_fraud", Boolean, nullable=False),
    Column("hypothesis_key", Text, nullable=False),
    Column("coached", Boolean, nullable=False),
    _ts("created_at"),
)

account_truth = Table(
    "account_truth",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    Column("is_mule", Boolean, nullable=False),
    Column("ring_id", Uuid),
    Column("layer", SmallInteger),
    _ts("created_at"),
)

response_overrides = Table(
    "response_overrides",
    metadata,
    Column("scenario_type", Text, primary_key=True),
    Column("source_key", Text, primary_key=True),
    Column("coached", Boolean, primary_key=True),
    Column("probs", JSONB, nullable=False),
    _ts("created_at"),
)

label_schedule = Table(
    "label_schedule",
    metadata,
    Column("subject_type", Text, primary_key=True),
    Column("subject_id", Uuid, primary_key=True),
    Column("hypothesis_key", Text, nullable=False),
    Column("label_source", Text, primary_key=True),
    _ts("due_at"),
    Column("delivered", Boolean, nullable=False),
    _ts("created_at"),
)

# Why each bank.sim_schedule TRANSFER row exists (migration 0004, #56).
schedule_truth = Table(
    "schedule_truth",
    metadata,
    Column("schedule_id", Uuid, primary_key=True),
    Column("reason", Text, nullable=False),
    Column("flow", Integer),
    Column("session", Boolean, nullable=False),
    _ts("created_at"),
)

# The persona manifest of a dataset (migration 0005, docs/14 §6): it names the scripted rings
# and the story payees, so it is ground truth.
persona_manifests = Table(
    "persona_manifests",
    metadata,
    Column("dataset_id", Uuid, primary_key=True),
    Column("manifest", JSONB, nullable=False),
    _ts("created_at"),
)
