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

The integration tests create and drop their own databases on the server you point them at, so
never point `DELPHI_TEST_DATABASE_URL` at a database you care about. All settings are listed in
[docs/23 §4.1](docs/23-deployment.md#41-process-settings).
