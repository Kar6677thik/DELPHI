# Contributing to DELPHI

The full guide is on the wiki: **[Contributing](https://github.com/Kar6677thik/DELPHI/wiki/Contributing)**.
The short version:

1. **Pick an issue** from the [issues](https://github.com/Kar6677thik/DELPHI/issues). Work
   is grouped by [milestone](https://github.com/Kar6677thik/DELPHI/milestones) (M0–M9,
   `docs/25-milestones.md`). Assign it to yourself before starting. If what you want to do has
   no issue, open one first.
2. **Read the spec sections** the issue links. `docs/` is the source of truth. If the code
   needs to differ, change the spec in the same PR (plus an ADR for architectural changes).
3. **Branch** from `main` as `m<N>/<issue>-<short-topic>`, e.g. `m2/17-mule-networks`.
4. **Enable the hooks once:** `git config core.hooksPath .githooks`.
5. **Commit** with [Conventional Commits](https://www.conventionalcommits.org/) (`feat(sim): …`).
   No AI attribution trailers (`Co-Authored-By: …`, "Generated with …"); the hook rejects them.
6. **Open a PR** using the template. Keep it small (≈ 400 changed lines). CI must be green, and
   the owner reviews (`.github/CODEOWNERS`). PRs are squash-merged.

Using a coding agent (Claude Code, Codex, Cursor, …)? It must follow [`AGENTS.md`](AGENTS.md).

## Local setup

Python 3.12 and [uv](https://docs.astral.sh/uv/); Docker for PostgreSQL. More detail is on the
wiki's [Getting Started](https://github.com/Kar6677thik/DELPHI/wiki/Getting-Started) page.

```sh
git config core.hooksPath .githooks
cd backend && uv sync --frozen
uv run pytest                       # unit, property and golden tests; integration tests skip

docker compose up -d postgres       # from the repo root: PostgreSQL 16 on localhost:5432
export DELPHI_TEST_DATABASE_URL=postgresql://postgres:dev-only@localhost:5432/postgres
uv run pytest                       # now including the integration tests

export DELPHI_DATABASE_URL=postgresql://postgres:dev-only@localhost:5432/delphi
uv run delphi migrate && uv run delphi api   # http://localhost:8000/api/v1/ready
```

Or run the whole stack in Docker, from the production image (docs/23 §10):

```sh
docker compose up --build   # postgres, migrate, api :8000, worker, Vite :5173 (hot reload)
docker compose exec api delphi seed --if-empty --profile test --personas
```

Sign in at http://localhost:5173 as `admin` / `dev-only-admin-pw`. `docker compose watch`
rebuilds the api and worker when `backend/` or `kb/` changes. If you used the older,
postgres-only compose file, run `docker compose down -v` once, so Postgres creates the app's
login roles on a fresh volume.

The integration tests create and drop their own databases on the server you point them at, so
never point `DELPHI_TEST_DATABASE_URL` at a database you care about. All settings are listed in
[docs/23 §4.1](docs/23-deployment.md#41-process-settings).

### Sign in locally (frontend against the real api)

Node 22 and pnpm 9 for the frontend (docs/24 §3). With PostgreSQL up and the database migrated
as above:

```sh
export DELPHI_BOOTSTRAP_ADMIN_USERNAME=admin               # created on start if no user exists
export DELPHI_BOOTSTRAP_ADMIN_PASSWORD='a long local password'
uv run delphi api                                          # backend/, on :8000

cd frontend && pnpm install --frozen-lockfile && pnpm dev  # http://localhost:5173
```

Open http://localhost:5173 in Chrome or Firefox and sign in as `admin`.

- **CSRF:** Vite proxies `/api` to the api with `changeOrigin` off, so the api sees the
  browser's own `Origin` and `Host` (`localhost:5173`) and its same-origin check passes
  (docs/16 §1). Leave `DELPHI_PUBLIC_ORIGIN` unset locally; set to anything else, every
  sign-in gets `403 CSRF_REJECTED`.
- **The cookie is `Secure`:** Chrome and Firefox keep it on `http://localhost`. Safari may not,
  and then every page sends you back to sign in.
- **Lockout:** five wrong passwords for one username from one address within 15 minutes lock
  sign-in; the page says when to try again (`429 RATE_LIMITED`, `Retry-After`). Restart the api
  to clear it.

The frontend tests answer the API from MSW (`frontend/tests/mocks`); the app itself always talks
to the api.

### A dataset locally

`delphi seed` queues `GENERATE_DATASET`, and the worker generates the profile and writes it
(docs/14, #19). The worker writes ground truth, so it needs the `delphi_sim` pool too. Locally,
the single superuser can stand in for both roles:

```sh
export DELPHI_SIM_DATABASE_URL=$DELPHI_DATABASE_URL
uv run delphi seed --if-empty --profile test --personas   # or demo (about a minute)
uv run delphi worker                                      # Ctrl-C once the job has SUCCEEDED
```

A database holds one dataset. Seeding again is harmless (`--if-empty`); to start over, drop and
recreate the database.
