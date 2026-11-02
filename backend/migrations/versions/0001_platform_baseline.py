"""Platform baseline: roles, schemas and the platform tables.

Revision ID: 0001
Revises:
Create Date: 2026-11-02

Creates the database roles and the three schemas with their grants (docs/17 §2), and the
platform tables `users`, `auth_sessions`, `idempotency_keys`, `audit_log`, `jobs` and
`outbox_events` (docs/17 §3).

Roles are cluster-wide. In the home deployment the Postgres init script creates them as LOGIN
roles with passwords from the Secret `delphi-db` (docs/23 §4); where they do not exist yet
(CI, a fresh local database) this migration creates them as NOLOGIN, which is enough for the
grants and for `SET ROLE` in tests. Migrations always run as the same owner role, because
default privileges belong to the role that creates the tables.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "delphi_app"
SIM_ROLE = "delphi_sim"
DML = "SELECT, INSERT, UPDATE, DELETE"

ROLES_AND_SCHEMAS = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    CREATE ROLE {APP_ROLE} NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{SIM_ROLE}') THEN
    CREATE ROLE {SIM_ROLE} NOLOGIN;
  END IF;
END
$$;

CREATE SCHEMA delphi;
CREATE SCHEMA bank;
CREATE SCHEMA sim_truth;

-- delphi_app (api and worker): product and bank tables. No grant at all on sim_truth.
GRANT USAGE ON SCHEMA delphi, bank TO {APP_ROLE};
-- delphi_sim (sim and evaluation code paths in the worker): everything, including ground truth.
GRANT USAGE ON SCHEMA delphi, bank, sim_truth TO {SIM_ROLE};

ALTER DEFAULT PRIVILEGES IN SCHEMA delphi GRANT {DML} ON TABLES TO {APP_ROLE}, {SIM_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA delphi GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}, {SIM_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA bank GRANT {DML} ON TABLES TO {APP_ROLE}, {SIM_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA bank GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}, {SIM_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA sim_truth GRANT {DML} ON TABLES TO {SIM_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA sim_truth GRANT USAGE, SELECT ON SEQUENCES TO {SIM_ROLE};

-- /ready compares the schema revision with the migration head (docs/20 §3).
GRANT SELECT ON public.alembic_version TO {APP_ROLE}, {SIM_ROLE};
"""

TABLES = """
CREATE TABLE delphi.users (
  id            uuid PRIMARY KEY,
  username      text NOT NULL UNIQUE CHECK (char_length(username) BETWEEN 1 AND 64),
  display_name  text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 128),
  password_hash text NOT NULL,
  role          text NOT NULL CHECK (role IN ('VIEWER', 'INVESTIGATOR', 'FDE_ENGINEER', 'ADMIN')),
  disabled      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  version       integer NOT NULL DEFAULT 1
);

-- The id is the SHA-256 of the 256-bit session token; the token itself is never stored.
CREATE TABLE delphi.auth_sessions (
  id           bytea PRIMARY KEY CHECK (octet_length(id) = 32),
  user_id      uuid NOT NULL REFERENCES delphi.users (id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  ip           inet,
  user_agent   text CHECK (char_length(user_agent) <= 512)
);
CREATE INDEX auth_sessions_user_id_idx ON delphi.auth_sessions (user_id);
CREATE INDEX auth_sessions_expires_at_idx ON delphi.auth_sessions (expires_at);

CREATE TABLE delphi.idempotency_keys (
  key             uuid NOT NULL,
  user_id         uuid NOT NULL REFERENCES delphi.users (id) ON DELETE CASCADE,
  endpoint        text NOT NULL,
  request_sha256  bytea NOT NULL CHECK (octet_length(request_sha256) = 32),
  response_status integer NOT NULL CHECK (response_status BETWEEN 100 AND 599),
  response_body   jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key, user_id)
);
CREATE INDEX idempotency_keys_user_id_idx ON delphi.idempotency_keys (user_id);
CREATE INDEX idempotency_keys_created_at_idx ON delphi.idempotency_keys (created_at);

-- Append-only and hash-chained (docs/17 §4). `at` is the entry's creation time and is part of
-- the hashed content, so the writer supplies it.
CREATE TABLE delphi.audit_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL,
  actor_type  text NOT NULL CHECK (actor_type IN ('USER', 'SYSTEM', 'ENGINE', 'LLM', 'JOB')),
  actor_id    text,
  action      text NOT NULL,
  entity_type text NOT NULL,
  entity_id   text,
  payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
  prev_hash   bytea NOT NULL CHECK (octet_length(prev_hash) = 32),
  hash        bytea NOT NULL UNIQUE CHECK (octet_length(hash) = 32)
);
CREATE INDEX audit_log_entity_idx ON delphi.audit_log (entity_type, entity_id);
CREATE INDEX audit_log_at_idx ON delphi.audit_log (at);

CREATE TABLE delphi.jobs (
  id              uuid PRIMARY KEY,
  type            text NOT NULL,
  status          text NOT NULL DEFAULT 'QUEUED'
                  CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
  params          jsonb NOT NULL DEFAULT '{}'::jsonb,
  result          jsonb,
  error           text,
  progress        real NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts    integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  idempotency_key text NOT NULL UNIQUE,
  locked_by       text,
  locked_until    timestamptz,
  run_after       timestamptz NOT NULL DEFAULT now(),
  created_by      uuid REFERENCES delphi.users (id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz
);
CREATE INDEX jobs_status_run_after_idx ON delphi.jobs (status, run_after);
CREATE INDEX jobs_created_by_idx ON delphi.jobs (created_by);

-- Transactional outbox for SSE (docs/04 §5.5). `at` is the creation time.
CREATE TABLE delphi.outbox_events (
  id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  type      text NOT NULL,
  entity_id text,
  payload   jsonb NOT NULL DEFAULT '{}'::jsonb,
  at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_events_at_idx ON delphi.outbox_events (at);
"""

# The default privileges grant full DML on new tables; the audit log is INSERT/SELECT only
# for both application roles (docs/17 §2, docs/19 §1 "Tampering").
AUDIT_APPEND_ONLY = f"""
REVOKE UPDATE, DELETE, TRUNCATE ON delphi.audit_log FROM {APP_ROLE}, {SIM_ROLE};
"""


def upgrade() -> None:
    op.execute(ROLES_AND_SCHEMAS)
    op.execute(TABLES)
    op.execute(AUDIT_APPEND_ONLY)


def downgrade() -> None:
    for table in ("outbox_events", "jobs", "audit_log", "idempotency_keys", "auth_sessions",
                  "users"):
        op.execute(f"DROP TABLE delphi.{table}")
    op.execute(f"REVOKE SELECT ON public.alembic_version FROM {APP_ROLE}, {SIM_ROLE}")
    for schema, roles in (("delphi", f"{APP_ROLE}, {SIM_ROLE}"), ("bank", f"{APP_ROLE}, {SIM_ROLE}"),
                          ("sim_truth", SIM_ROLE)):
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {roles}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON SEQUENCES FROM {roles}"
        )
        op.execute(f"DROP SCHEMA {schema}")
    # The roles are cluster-wide and may hold grants in other databases: they are not dropped.
