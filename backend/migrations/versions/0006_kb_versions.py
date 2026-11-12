"""KB versions (docs/07 §1, docs/17 §3, #15).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11

`delphi.kb_versions` keeps every KB version as canonical JSON with its SHA-256. Versions are
immutable: only the lifecycle columns (status, approval, activation, retirement) ever change.
Two partial unique indexes enforce FR-KB-02: exactly one ACTIVE version (at most one here;
`delphi migrate` inserts KB v1 as the first) and at most one SHADOW.

`created_by` and `approved_by` are NULL for system actions, such as `delphi migrate` loading
KB v1. 0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.kb_versions (
  id              serial PRIMARY KEY,
  status          text NOT NULL
                  CHECK (status IN ('CANDIDATE', 'SHADOW', 'ACTIVE', 'RETIRED', 'REJECTED')),
  parent_id       integer REFERENCES delphi.kb_versions (id),
  content         jsonb NOT NULL,
  content_sha256  text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  change_summary  text NOT NULL,
  created_by      uuid REFERENCES delphi.users (id) ON DELETE SET NULL,
  approved_by     uuid REFERENCES delphi.users (id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL,
  activated_at    timestamptz,
  retired_at      timestamptz,
  rollback_reason text,
  version         integer NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE UNIQUE INDEX kb_versions_one_active_idx ON delphi.kb_versions (status)
  WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX kb_versions_one_shadow_idx ON delphi.kb_versions (status)
  WHERE status = 'SHADOW';
CREATE INDEX kb_versions_parent_id_idx ON delphi.kb_versions (parent_id);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.kb_versions")
