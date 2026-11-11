"""Sensing model artifacts and the mule scores they produce (docs/13 §7, docs/17 §3, #88).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11

`delphi.model_artifacts` keeps every trained sensing model (the prior, the anomaly detector, the
mule scorer): its joblib blob with the blob's SHA-256, the metrics the gate read, the feature
list and whether the gate passed. A partial unique index allows one ACTIVE artifact per kind.
`gate_passed` is false on an artifact that was activated although it failed its gate, which
only the first artifact of a kind may be (docs/13 §4).

`bank.account_scores` holds each account's p_mule and the artifact that computed it. It waited
for this table, which its foreign key needs (TD-7).

0001's default privileges give both application roles DML on both tables. Expand/contract
(docs/17 §1): this migration only adds tables.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE delphi.model_artifacts (
  id              uuid PRIMARY KEY,
  kind            text NOT NULL CHECK (kind IN ('PRIOR', 'ANOMALY', 'MULE')),
  version         integer NOT NULL CHECK (version >= 1),
  status          text NOT NULL CHECK (status IN ('ACTIVE', 'RETIRED', 'FAILED_GATE')),
  gate_passed     boolean NOT NULL,
  dataset_id      uuid REFERENCES delphi.datasets (id) ON DELETE SET NULL,
  job_id          uuid REFERENCES delphi.jobs (id) ON DELETE SET NULL,
  trained_at      timestamptz NOT NULL,
  training_cutoff timestamptz NOT NULL,
  metrics         jsonb NOT NULL,
  feature_list    jsonb NOT NULL,
  blob            bytea NOT NULL,
  sha256          bytea NOT NULL CHECK (octet_length(sha256) = 32),
  activated_at    timestamptz,
  retired_at      timestamptz,
  UNIQUE (kind, version)
);

CREATE UNIQUE INDEX model_artifacts_one_active_idx ON delphi.model_artifacts (kind)
  WHERE status = 'ACTIVE';

CREATE TABLE bank.account_scores (
  account_id        uuid PRIMARY KEY REFERENCES bank.accounts (id) ON DELETE CASCADE,
  p_mule            double precision NOT NULL CHECK (p_mule >= 0 AND p_mule <= 1),
  computed_at       timestamptz NOT NULL,
  model_artifact_id uuid NOT NULL REFERENCES delphi.model_artifacts (id)
);

CREATE INDEX account_scores_model_artifact_id_idx ON bank.account_scores (model_artifact_id);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP TABLE bank.account_scores")
    op.execute("DROP TABLE delphi.model_artifacts")
