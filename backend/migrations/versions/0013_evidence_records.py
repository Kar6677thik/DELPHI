"""Evidence records (docs/09 §3, docs/17, #102).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-18

`delphi.evidence_records` holds what each evidence fetch of a case found: the source, its
outcome, the facts and record ids the adapter used (the only material a narrative may cite,
docs/09 §4), the posterior before and after, and the source's rank and net value when it was
requested. A fetch is idempotent per (case, source): a refresh supersedes the current record
(`superseded_by`), so at most one record per (case, source) is current (a partial unique index).
The narrative columns stay null until the narrative job (docs/15) fills them.

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table and its indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.evidence_records (
  id                         uuid PRIMARY KEY,
  case_id                    uuid NOT NULL REFERENCES delphi.cases (id),
  source_key                 text NOT NULL,
  outcome                    text NOT NULL,
  raw                        jsonb NOT NULL,
  record_refs                jsonb NOT NULL,
  fetch_ms                   integer NOT NULL CONSTRAINT evidence_records_fetch_ms_check
                             CHECK (fetch_ms >= 0),
  posterior_before           jsonb NOT NULL,
  posterior_after            jsonb NOT NULL,
  info_yield_bits            double precision NOT NULL
                             CONSTRAINT evidence_records_info_yield_check
                             CHECK (info_yield_bits >= 0),
  rank_at_request            integer CONSTRAINT evidence_records_rank_check
                             CHECK (rank_at_request >= 1),
  net_value_at_request_paise bigint,
  requested_by               uuid REFERENCES delphi.users (id),
  requested_at               timestamptz NOT NULL,
  narrative                  text,
  narrative_source           text CONSTRAINT evidence_records_narrative_source_check
                             CHECK (narrative_source IN ('LLM', 'TEMPLATE')),
  narrative_citations        jsonb,
  -- Deferred: a refresh points the current record at its successor before inserting it, since
  -- the partial unique index allows one current record per (case, source).
  superseded_by              uuid REFERENCES delphi.evidence_records (id)
                             DEFERRABLE INITIALLY DEFERRED,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT evidence_records_narrative_check CHECK (
    (narrative IS NULL) = (narrative_source IS NULL)
  )
);
-- docs/09 §3: one current record per (case, source); a refresh supersedes it.
CREATE UNIQUE INDEX evidence_records_current_key ON delphi.evidence_records (case_id, source_key)
  WHERE superseded_by IS NULL;
-- The case's evidence cards, in the order they were fetched; foreign keys are indexed.
CREATE INDEX evidence_records_case_requested_at_idx
  ON delphi.evidence_records (case_id, requested_at, id);
CREATE INDEX evidence_records_requested_by_idx ON delphi.evidence_records (requested_by);
CREATE INDEX evidence_records_superseded_by_idx ON delphi.evidence_records (superseded_by);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.evidence_records")
