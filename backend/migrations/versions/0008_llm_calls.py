"""LLM calls (docs/15, docs/17 §3, #104).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11

`delphi.llm_calls` records every attempt to produce text, by a model or by the template: the
purpose, provider, model and prompt version, the minimised request and the output, the
validation result, the tokens and the latency. The daily token budget is read from it (docs/15
§3), and RETENTION_SWEEP nulls the request and response after 30 days (docs/17 §6).

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.llm_calls (
  id               uuid PRIMARY KEY,
  purpose          text NOT NULL CHECK (purpose IN
                   ('EVIDENCE_NARRATIVE', 'CASE_SUMMARY', 'QUESTION_VARIANTS',
                    'TYPOLOGY_DESCRIPTION')),
  provider         text NOT NULL,
  model            text,
  prompt_version   text NOT NULL,
  request_redacted jsonb,
  response         jsonb,
  status           text NOT NULL CHECK (status IN
                   ('OK', 'REFUSED', 'INVALID_OUTPUT', 'ERROR', 'BUDGET_EXCEEDED', 'DISABLED')),
  validation       jsonb NOT NULL,
  input_tokens     integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens    integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  latency_ms       integer NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
  created_at       timestamptz NOT NULL
);

CREATE INDEX llm_calls_created_at_idx ON delphi.llm_calls (created_at);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.llm_calls")
