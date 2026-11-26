"""Scout runs, clusters, typology drafts and queue dismissals (docs/11 §1–2, docs/17, #117).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-23

`RUN_SCOUT` stores each run, its clusters and one CANDIDATE `typology_drafts` row per cluster
(docs/11 §2); #118's draft editor and backtests build on the same table. A later run marks the
earlier runs' drafts still CANDIDATE as SUPERSEDED (the owner's decision on #255). An FDE engineer can
dismiss an unexplained-queue item with a reason (docs/11 §1): one dismissal per item.

It also indexes what the Scout reads every run (bounded after #263): the unexplained Guardian
sessions by `decided_at` (partial, `WHERE unexplained`), the reference set of explained decided
sessions, newest first (partial, `WHERE NOT unexplained AND decided_at IS NOT NULL`), the
disposed account reviews by `disposed_at`, the clusters by `(created_at, id)` for
`GET /scout/clusters`, and the members of clusters (GIN on `member_refs`, for the
promoted-member test).
One run per job: a retried `RUN_SCOUT` finds its run by `job_id` (unique) instead of inserting
a second one.

0001's default privileges give both application roles DML on the tables. Expand/contract
(docs/17 §1): this migration only adds tables and their indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE delphi.scout_runs (
  id          uuid PRIMARY KEY,
  job_id      uuid REFERENCES delphi.jobs (id) ON DELETE SET NULL,
  started_at  timestamptz NOT NULL,
  finished_at timestamptz,
  n_items     integer NOT NULL CHECK (n_items >= 0),
  n_clusters  integer NOT NULL CHECK (n_clusters >= 0),
  params      jsonb NOT NULL
);
CREATE INDEX scout_runs_started_idx ON delphi.scout_runs (started_at);
CREATE UNIQUE INDEX scout_runs_job_key ON delphi.scout_runs (job_id);

CREATE TABLE delphi.scout_clusters (
  id                      uuid PRIMARY KEY,
  run_id                  uuid NOT NULL REFERENCES delphi.scout_runs (id),
  size                    integer NOT NULL CHECK (size >= 1),
  member_refs             jsonb NOT NULL,
  distinguishing_features jsonb NOT NULL,
  outcome_profile         jsonb NOT NULL,
  created_at              timestamptz NOT NULL
);
CREATE INDEX scout_clusters_run_idx ON delphi.scout_clusters (run_id);
CREATE INDEX scout_clusters_created_idx ON delphi.scout_clusters (created_at, id);
CREATE INDEX scout_clusters_member_refs_idx ON delphi.scout_clusters
  USING gin (member_refs jsonb_path_ops);

CREATE TABLE delphi.typology_drafts (
  id                   uuid PRIMARY KEY,
  cluster_id           uuid REFERENCES delphi.scout_clusters (id),
  status               text NOT NULL CHECK (status IN ('CANDIDATE', 'DRAFT', 'BACKTESTED',
                         'IN_SHADOW', 'PROMOTED', 'REJECTED', 'SUPERSEDED')),
  revision             integer NOT NULL CHECK (revision >= 1),
  spec                 jsonb NOT NULL,
  last_edited_by       uuid REFERENCES delphi.users (id),
  -- docs/05 §7: everyone who ever saved a revision (four-eyes promotion).
  editors              uuid[] NOT NULL DEFAULT '{}',
  backtest_report      jsonb,
  backtested_revision  integer,
  shadow_kb_version_id integer REFERENCES delphi.kb_versions (id),
  approved_by          uuid REFERENCES delphi.users (id),
  rejected_reason      text,
  created_at           timestamptz NOT NULL,
  updated_at           timestamptz NOT NULL,
  version              integer NOT NULL CHECK (version >= 1)
);
CREATE INDEX typology_drafts_status_idx ON delphi.typology_drafts (status, updated_at);
CREATE INDEX typology_drafts_cluster_idx ON delphi.typology_drafts (cluster_id);
CREATE INDEX typology_drafts_last_edited_by_idx ON delphi.typology_drafts (last_edited_by);
CREATE INDEX typology_drafts_shadow_kb_idx ON delphi.typology_drafts (shadow_kb_version_id);
CREATE INDEX typology_drafts_approved_by_idx ON delphi.typology_drafts (approved_by);
-- docs/05 §7: only one draft is IN_SHADOW at a time.
CREATE UNIQUE INDEX typology_drafts_one_in_shadow_idx ON delphi.typology_drafts ((true))
  WHERE status = 'IN_SHADOW';

CREATE TABLE delphi.scout_queue_dismissals (
  -- subject_id has no foreign key: a Guardian session or a case (polymorphic, docs/17 §1).
  subject_type  text NOT NULL CHECK (subject_type IN ('GUARDIAN_SESSION', 'CASE')),
  subject_id    uuid NOT NULL,
  reason        text NOT NULL CHECK (reason <> ''),
  dismissed_by  uuid NOT NULL REFERENCES delphi.users (id),
  dismissed_at  timestamptz NOT NULL,
  PRIMARY KEY (subject_type, subject_id)
);
CREATE INDEX scout_queue_dismissals_by_idx ON delphi.scout_queue_dismissals (dismissed_by);
CREATE INDEX guardian_sessions_unexplained_decided_idx ON delphi.guardian_sessions
  (decided_at, id) WHERE unexplained;
CREATE INDEX guardian_sessions_reference_decided_idx ON delphi.guardian_sessions
  (decided_at DESC, id) WHERE NOT unexplained AND decided_at IS NOT NULL;
CREATE INDEX cases_disposed_review_idx ON delphi.cases (disposed_at, id)
  WHERE type = 'ACCOUNT_REVIEW' AND status = 'DISPOSED';
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP INDEX delphi.cases_disposed_review_idx")
    op.execute("DROP INDEX delphi.guardian_sessions_reference_decided_idx")
    op.execute("DROP INDEX delphi.guardian_sessions_unexplained_decided_idx")
    op.execute(
        "DROP TABLE delphi.scout_queue_dismissals, delphi.typology_drafts,"
        " delphi.scout_clusters, delphi.scout_runs"
    )
