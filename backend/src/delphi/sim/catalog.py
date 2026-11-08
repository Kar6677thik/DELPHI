"""The datasets written into the bank (docs/17 §3, migration 0005).

A row per dataset the ``GENERATE_DATASET`` job wrote. It holds no ground truth, so the api may
read it (to refuse a second dataset, docs/16 §2) as well as the worker.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(schema="delphi")

READY = "READY"

datasets = Table(
    "datasets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("job_id", Uuid, nullable=False, unique=True),
    Column("profile", Text, nullable=False),
    Column("seed", BigInteger, nullable=False),
    Column("generator_version", Integer, nullable=False),
    # The job writes a dataset in one transaction, so a row only ever exists READY.
    Column("status", Text, nullable=False),
    Column("stats", JSONB, nullable=False),
    Column("seed_completed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
