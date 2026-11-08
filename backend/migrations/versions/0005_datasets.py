"""Generated datasets and their persona manifests.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08

`delphi.datasets` records each dataset the `GENERATE_DATASET` job wrote into the bank (docs/17
§3, #19): its profile, seed and generator version, its stats report, and `seed_completed_at`,
the marker demo reset deletes back to (docs/14 §10). `job_id` lets a retried job find the
dataset its earlier attempt committed, so the handler is idempotent.

`sim_truth.persona_manifests` keeps the persona manifest (docs/14 §6, TD-9) for the story
runners. It lives in `sim_truth` because it names Ring-7's accounts and Kamala's mule payee;
the api finds the personas themselves through `bank.customers.persona_key`.

0001's default privileges give both roles DML on `delphi.datasets` and only `delphi_sim` any
access to `sim_truth`. Expand/contract (docs/17 §1): this migration only adds tables.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE delphi.datasets (
  id                uuid PRIMARY KEY,
  job_id            uuid NOT NULL UNIQUE REFERENCES delphi.jobs (id),
  profile           text NOT NULL CHECK (profile IN ('test', 'demo', 'eval')),
  seed              bigint NOT NULL CHECK (seed >= 0),
  generator_version integer NOT NULL CHECK (generator_version >= 1),
  status            text NOT NULL CHECK (status IN ('READY')),
  stats             jsonb NOT NULL,
  seed_completed_at timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sim_truth.persona_manifests (
  dataset_id uuid PRIMARY KEY REFERENCES delphi.datasets (id) ON DELETE CASCADE,
  manifest   jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP TABLE sim_truth.persona_manifests")
    op.execute("DROP TABLE delphi.datasets")
