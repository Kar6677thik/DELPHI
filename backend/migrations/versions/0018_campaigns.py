"""Campaigns (docs/05 §5, docs/09 §8, docs/17, #107).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-24

`CAMPAIGN_RECOMPUTE` stores each campaign the merge rule finds (`delphi.navigator.campaign_store`)
and sets `cases.campaign_id`, which now gets its foreign key (0012 left it without one). A
campaign that merged into an older one keeps its row with `merged_into` set, so a stale link
still resolves; one left with no group keeps its row with `case_count = 0`.

`case_entities` holds each case's infrastructure entities (docs/05 §5), written once when the job
first sees the case, so a case's neighbours are one indexed lookup. `campaign_watermark` is the
job's position in the stream of touched cases (opened, disposed, or with new evidence); the
indexes on `cases.disposed_at` and `evidence_records.requested_at` (Clock time, like the cases'
times) serve that scan. The list index matches `GET /campaigns`' filter and order, so a page is
an index range with no sort, and merged or dissolved rows stay out of it.

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds tables, indexes and a foreign key. The foreign key is added
`NOT VALID` and validated separately, so checking the existing rows does not block writes to
`cases`.
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.campaigns (
  id                   uuid PRIMARY KEY,
  leading_hypothesis   text NOT NULL,
  case_count           integer NOT NULL CHECK (case_count >= 0),
  f3_stage_max         text,
  expected_loss_paise  bigint NOT NULL CHECK (expected_loss_paise >= 0),
  vulnerable_victims   integer NOT NULL CHECK (vulnerable_victims >= 0),
  rank_score           double precision NOT NULL CHECK (rank_score >= 0),
  merged_into          uuid REFERENCES delphi.campaigns (id),
  created_at           timestamptz NOT NULL,
  updated_at           timestamptz NOT NULL,
  CHECK (merged_into IS NULL OR merged_into <> id)
);
CREATE INDEX campaigns_rank_idx ON delphi.campaigns (rank_score DESC, id DESC)
  WHERE merged_into IS NULL AND case_count > 0;
CREATE INDEX campaigns_merged_into_idx ON delphi.campaigns (merged_into);
ALTER TABLE delphi.cases
  ADD CONSTRAINT cases_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES delphi.campaigns (id)
  ON DELETE SET NULL NOT VALID;
ALTER TABLE delphi.cases VALIDATE CONSTRAINT cases_campaign_id_fkey;
CREATE INDEX cases_campaign_id_idx ON delphi.cases (campaign_id);
CREATE TABLE delphi.case_entities (
  case_id  uuid NOT NULL REFERENCES delphi.cases (id) ON DELETE CASCADE,
  entity   text NOT NULL CHECK (entity ~ '^(device|account|ip24):.+'),
  PRIMARY KEY (case_id, entity)
);
CREATE INDEX case_entities_entity_idx ON delphi.case_entities (entity, case_id);
CREATE TABLE delphi.campaign_watermark (
  id       smallint PRIMARY KEY CHECK (id = 1),
  at       timestamptz NOT NULL,
  case_id  uuid NOT NULL
);
INSERT INTO delphi.campaign_watermark VALUES (1, '1970-01-01T00:00:00Z',
  '00000000-0000-0000-0000-000000000000');
CREATE INDEX cases_disposed_at_idx ON delphi.cases (disposed_at, id) WHERE disposed_at IS NOT NULL;
CREATE INDEX evidence_records_requested_at_idx ON delphi.evidence_records (requested_at, case_id);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP INDEX delphi.evidence_records_requested_at_idx")
    op.execute("DROP INDEX delphi.cases_disposed_at_idx")
    op.execute("DROP TABLE delphi.campaign_watermark, delphi.case_entities")
    op.execute("DROP INDEX delphi.cases_campaign_id_idx")
    op.execute("ALTER TABLE delphi.cases DROP CONSTRAINT cases_campaign_id_fkey")
    op.execute("DROP TABLE delphi.campaigns")
