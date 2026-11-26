"""Question variants drafted in the app, waiting for review (docs/07 §9, docs/15, #110).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-26

`LLM_QUESTION_VARIANTS` stores the variants it drafts as PENDING_REVIEW. An FDE approves (with
an optional edit) or rejects each one; "approve batch" puts the APPROVED ones not yet included
into a CANDIDATE KB version (`included_in_kb_version`). Nothing here is served: the Guardian
serves only the question bank of the KB version a session is pinned to (FR-KB-05).

`job_id` makes the drafting job idempotent: a retry finds the variants its earlier attempt
committed. `reviewed_by` has no foreign key, as `cases.acted_by`; the audit chain names the
reviewer too.

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table and its indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE delphi.question_variants (
  id                      uuid PRIMARY KEY,
  probe_key               text NOT NULL,
  language                text NOT NULL CHECK (language IN ('en', 'hi', 'te', 'ta')),
  text                    text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 300),
  options                 jsonb NOT NULL CHECK (jsonb_typeof(options) = 'array'),
  status                  text NOT NULL
                          CHECK (status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
  generated_by            text NOT NULL CHECK (generated_by IN ('HUMAN', 'LLM')),
  llm_call_id             uuid REFERENCES delphi.llm_calls (id),
  job_id                  uuid,
  reviewed_by             uuid,
  reviewed_at             timestamptz,
  included_in_kb_version  integer REFERENCES delphi.kb_versions (id),
  created_at              timestamptz NOT NULL,
  CHECK ((status = 'PENDING_REVIEW') = (reviewed_at IS NULL)),
  CHECK (included_in_kb_version IS NULL OR status = 'APPROVED'),
  CHECK ((generated_by = 'LLM') = (llm_call_id IS NOT NULL))
)
"""
    )
    op.execute(
        "CREATE INDEX question_variants_status_idx"
        " ON delphi.question_variants (status, created_at, id)"
    )
    op.execute("CREATE INDEX question_variants_job_idx ON delphi.question_variants (job_id)")
    op.execute(
        "CREATE INDEX question_variants_kb_version_idx"
        " ON delphi.question_variants (included_in_kb_version)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE delphi.question_variants")
