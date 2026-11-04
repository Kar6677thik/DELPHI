# 24 — Engineering conventions

How the engineering field guide (`reference/engineering-field-guide.md`) applies to this repo.
When a guide principle and this document conflict, this document wins for DELPHI.

## 1. Principles in force (and how they show up)

| Principle (EFG) | Applied as |
|---|---|
| Modular monolith (§5) | One package, module facades, import-linter contracts (doc 04) |
| Functional core / DIP (§2) | Pure `engine`; ports and adapters for every external system |
| KISS / YAGNI (§2) | PostgreSQL only; no broker, cache, tracing stack or k8s operators beyond Argo CD; no features outside doc 03 |
| Explicitness (§2) | Composition root wiring; no global singletons; config validated at startup |
| Fail fast (§2) | Invalid config or KB → refuse to start; invalid input → 4xx with a precise code |
| Design by contract (§2) | Pydantic DTOs; engine pre/post-conditions as asserts in tests; DB constraints |
| ADRs (§1) | `docs/adr/NNNN-title.md` for every architectural decision; superseded ADRs stay, marked |
| Idempotency, outbox (§11, §19) | Idempotency keys, idempotent jobs, transactional outbox for SSE |
| Expand/contract (§6) | Every migration is backward-compatible with the previous release |
| Testing portfolio (§23) | doc 21 |
| Observability (§24) | doc 20 |
| Security by default (§25) | doc 19 |
| LLM engineering (§29) | Structured outputs, grounding, validation, prompt versioning, fallback, no autonomous tools |
| Anti-patterns to avoid (§34) | No god objects (one aggregate per module); no premature microservices; no silent error handling; no hardcoded secrets; no mock-everything tests (integration tests use real Postgres) |

## 2. Repository layout

```
/
  CLAUDE.md  README.md  justfile  compose.yaml  Dockerfile  .editorconfig  .gitattributes
  .github/workflows/  .github/dependabot.yml  .github/pull_request_template.md
  backend/
    pyproject.toml  uv.lock  openapi.json  alembic.ini
    src/delphi/...        (doc 04 §4)
    migrations/
    tests/ unit/ property/ golden/ integration/ fixtures/ llm_eval/
  frontend/
    package.json  pnpm-lock.yaml  vite.config.ts  tsconfig.json
    src/ api/ components/ routes/ lib/
    tests/ e2e/
  kb/                     (doc 07 §2)
  deploy/                 (doc 23 §1)
  docs/                   (this spec)  docs/reports/  (generated reports, committed by the owner)
  security/waivers.yaml
```

## 3. Toolchain (pinned)

| Tool | Version policy |
|---|---|
| Python | 3.12.x |
| uv | Pinned in CI; `uv.lock` committed |
| Node | 22 LTS; `.nvmrc` |
| pnpm | 9.x via corepack; `pnpm-lock.yaml` committed |
| PostgreSQL | 16.x (same major locally, in CI and in prod) |
| just | Task runner (`winget install Casey.Just` on Windows). Each recipe is one line calling uv/pnpm/docker, so any command can be run without just |

Key backend libraries: fastapi, uvicorn, pydantic v2, pydantic-settings, sqlalchemy 2,
alembic, psycopg 3, numpy, pandas, scikit-learn, lightgbm, structlog, prometheus-client,
argon2-cffi, jinja2, anthropic. Dev: pytest, pytest-asyncio, hypothesis, coverage, ruff, mypy,
import-linter, locust, bandit, pip-audit. Adding a runtime dependency requires a line in the PR
description explaining why (make vs reuse, EFG §1).

## 4. Coding standards

- **Python:** ruff (rules E, F, I, B, UP, SIM, PL, RUF; line length 100), ruff format, type
  hints everywhere, `mypy --strict` for `engine`, `kb` and `chase.tracing`. Dataclasses
  (`frozen=True, slots=True`) for engine values; Pydantic for I/O DTOs. No `print`. Use the
  logger. Exceptions are domain-specific classes mapped to problem codes in one place.
- **Naming:** glossary terms (doc 02). Hypothesis/source/decision keys are string enums
  generated from the KB format, never re-typed as free strings in feature code.
- **TypeScript:** `strict`, `noUncheckedIndexedAccess`, no `any` (lint error), function
  components, hooks named `useX`, one route component per file.
- **SQL:** through SQLAlchemy Core/ORM only. Raw SQL only in migrations and the
  performance-critical tracing scan (parameterised `text()` with a test).
- **Comments:** explain *why*, not *what*. Every non-obvious maths line links to the doc
  section (e.g. `# doc 06 §6`).
- **Config:** every tunable number lives in the KB (`kb/*.yaml`), the sim config, or settings.
  No magic numbers in code (ruff `PLR2004` enabled; constants named).

## 5. Git workflow

- **Authorship (non-negotiable):** every commit is authored by the human contributor who made
  it, under their own git identity. No
  `Co-Authored-By` trailers, no AI attribution lines in commits or PRs, no `--author`
  overrides. CI never commits. A `commit-msg` hook in `.githooks/` (enabled with
  `git config core.hooksPath .githooks`) rejects messages containing `Co-Authored-By` or
  "Generated with Claude", and the CI job `commit-policy` checks every commit of a PR the same
  way. Cloud agents may skip local hooks, and squash merges copy commit messages onto `main`.
- **Planning lives in GitHub.** Every piece of work is an issue (templates in
  `.github/ISSUE_TEMPLATE/`), assigned to a milestone `M0`…`M9` that mirrors doc 25, and labelled
  `type:*`, `area:*` and, when useful, `P1`/`P2` or `status:*`. Each milestone has one
  `tracking` issue listing its deliverables. The **wiki** holds onboarding, how-tos and meeting
  notes, and it links to `docs/`. It never restates the spec: the spec stays in the repo, where
  it is versioned and reviewed with the code.
- Branches: `main` is protected. Work goes on `m<N>/<issue>-<short-topic>` (e.g.
  `m2/17-mule-networks`). Coding agents follow `AGENTS.md` (`CLAUDE.md` imports it).
- Commits: Conventional Commits (`feat(engine): add depth-limited lookahead`), imperative mood,
  ≤ 72-char subject.
- PRs: small (aim for ≤ 400 changed lines excluding generated files), squash-merged, with a
  template: what/why, spec sections touched, FR/NFR ids covered, test evidence, screenshots for
  UI. A spec change and its code change go in the same PR.
- Versioning: the deployed version is the git SHA. Milestone completions are marked by the owner
  with annotated tags `m0` … `m9` (manually; CI never tags).

## 6. Definition of Done (every PR)

1. Behaviour matches the spec sections listed in the PR, or the spec is updated in the same PR.
2. Tests at the right level are added. All CI gates are green.
3. No new warnings from linters or type checkers.
4. Docs: an ADR for any architectural decision; the glossary for any new term; the runbook for
   any new operational step.
5. Migrations are backward-compatible (expand/contract).
6. No secrets, no PII-like data in fixtures, no `TODO` without a linked issue.

## 7. Technical debt register

`docs/tech-debt.md` (created in M0). Each entry: description, why it was accepted, consequence,
trigger to repay. It is reviewed at every milestone close.
