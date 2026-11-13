"""Guardian sessions and their probe runs (docs/05 §2, docs/17 §3, #94 part 1).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-13

`delphi.guardian_sessions` holds one session per payment (`payment_id` UNIQUE), pinned to the
KB version it started with. `delphi.probe_runs` holds each probe the session ran. The docs/05 §2
invariants that a row can show are constraints:

- `decision` is set if and only if the session is DECIDED, and a DECIDED session has a
  `terminal_reason`; a customer-cancelled session has `terminal_reason = CUSTOMER_CANCELLED` and
  no decision;
- at most one probe run per session is AWAITING_CUSTOMER (a partial unique index);
- a probe runs at most once per session, and runs are numbered (`unique (session_id, seq)`).

Decision values come from the KB's decision profiles (docs/07), so they are not a CHECK here.
`case_id` has no foreign key yet: `cases` references this table too, so #97's migration adds
`guardian_sessions.case_id → cases.id` when it creates `cases`.

0001's default privileges give both application roles DML on both tables. Expand/contract
(docs/17 §1): this migration only adds tables.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE delphi.guardian_sessions (
  id                 uuid PRIMARY KEY,
  payment_id         uuid NOT NULL UNIQUE REFERENCES bank.payments (id),
  status             text NOT NULL CHECK (status IN
                     ('CREATED', 'PASSIVE_PROBING', 'PLANNING', 'AWAITING_CUSTOMER', 'DECIDED',
                      'CANCELLED_BY_CUSTOMER')),
  decision           text,
  terminal_reason    text CHECK (terminal_reason IN
                     ('STOP', 'FAST_PATH', 'BUDGET_EXHAUSTED', 'SESSION_TIMEOUT', 'ENGINE_ERROR',
                      'CUSTOMER_CANCELLED')),
  kb_version_id      integer NOT NULL REFERENCES delphi.kb_versions (id),
  prior              jsonb,
  p_model            jsonb,
  anomaly_percentile double precision CHECK (anomaly_percentile BETWEEN 0 AND 1),
  open_mass          double precision CHECK (open_mass BETWEEN 0 AND 1),
  open_mass_shares   jsonb,
  top_features       jsonb,
  posterior          jsonb,
  risk_by_decision   jsonb,
  unexplained        boolean NOT NULL DEFAULT false,
  mode               text NOT NULL CHECK (mode IN ('LIVE', 'AUTOPLAY', 'EVAL')),
  runner             text NOT NULL CHECK (runner IN ('API', 'WORKER')),
  rng_seed           bigint NOT NULL,
  started_at         timestamptz NOT NULL,
  decided_at         timestamptz,
  case_id            uuid,
  baseline_decisions jsonb,
  dd_pack_complete   boolean NOT NULL DEFAULT false,
  version            integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  CONSTRAINT guardian_sessions_decided_has_decision
    CHECK ((status = 'DECIDED') = (decision IS NOT NULL)),
  CONSTRAINT guardian_sessions_terminal_reason
    CHECK (
      (status = 'DECIDED' AND terminal_reason IS NOT NULL
         AND terminal_reason <> 'CUSTOMER_CANCELLED')
      OR (status = 'CANCELLED_BY_CUSTOMER' AND terminal_reason = 'CUSTOMER_CANCELLED')
      OR (status NOT IN ('DECIDED', 'CANCELLED_BY_CUSTOMER') AND terminal_reason IS NULL)
    ),
  CONSTRAINT guardian_sessions_decided_at
    CHECK ((status IN ('DECIDED', 'CANCELLED_BY_CUSTOMER')) = (decided_at IS NOT NULL))
);

CREATE INDEX guardian_sessions_kb_version_id_idx ON delphi.guardian_sessions (kb_version_id);
CREATE INDEX guardian_sessions_started_at_idx ON delphi.guardian_sessions (started_at);
CREATE INDEX guardian_sessions_case_id_idx ON delphi.guardian_sessions (case_id);

CREATE TABLE delphi.probe_runs (
  id                uuid PRIMARY KEY,
  session_id        uuid NOT NULL REFERENCES delphi.guardian_sessions (id) ON DELETE CASCADE,
  seq               integer NOT NULL CHECK (seq >= 1),
  probe_key         text NOT NULL,
  mode              text NOT NULL CHECK (mode IN ('PASSIVE', 'ACTIVE')),
  status            text NOT NULL CHECK (status IN
                    ('PENDING', 'AWAITING_CUSTOMER', 'COMPLETED', 'TIMED_OUT', 'ERROR')),
  selected_by       text CHECK (selected_by IN ('GREEDY', 'EXPLORE')),
  propensity        double precision CHECK (propensity > 0 AND propensity <= 1),
  eligible_set      jsonb,
  candidates        jsonb,
  lookahead_depth   integer CHECK (lookahead_depth >= 0),
  voi_myopic_paise  bigint,
  net_value_paise   bigint,
  cost_paise        bigint CHECK (cost_paise >= 0),
  variant_id        text,
  language          text CHECK (language IN ('en', 'hi', 'te', 'ta')),
  raw_response      jsonb,
  outcome           text,
  posterior_before  jsonb,
  posterior_after   jsonb,
  info_yield_bits   double precision,
  started_at        timestamptz NOT NULL,
  completed_at      timestamptz,
  UNIQUE (session_id, seq),
  UNIQUE (session_id, probe_key),
  -- Only the planner selects (active) probes; passive probes always run (docs/08 §3).
  CONSTRAINT probe_runs_passive_unselected
    CHECK (mode = 'ACTIVE' OR selected_by IS NULL),
  CONSTRAINT probe_runs_completed_has_outcome
    CHECK (status <> 'COMPLETED' OR outcome IS NOT NULL)
);

CREATE UNIQUE INDEX probe_runs_one_awaiting_idx ON delphi.probe_runs (session_id)
  WHERE status = 'AWAITING_CUSTOMER';
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.probe_runs")
    op.execute("DROP TABLE delphi.guardian_sessions")
