# Task runner (docs/24 §3). Each line is one plain command, so `just` is optional.
# Requires just >= 1.38 (working-directory attribute). Install on Windows: winget install Casey.Just
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

default:
    @just --list

# Install backend dependencies from the lockfile
[working-directory: 'backend']
sync:
    uv sync --frozen

# Lint, format check, types, module contracts (stops at the first failure)
[working-directory: 'backend']
check:
    uv run ruff check src tests
    uv run ruff format --check src tests
    uv run mypy
    uv run lint-imports

# Validate the knowledge base YAML (docs/07 §3)
[working-directory: 'backend']
kb-validate:
    uv run delphi kb validate ../kb

# Run the backend test suite with coverage gates
[working-directory: 'backend']
test:
    uv run coverage run -m pytest
    uv run coverage report --include="src/delphi/engine/*" --fail-under=95
    uv run coverage report --fail-under=80

# Install frontend dependencies from the lockfile
[working-directory: 'frontend']
fe-sync:
    pnpm install --frozen-lockfile

# Frontend lint, types, format, API types drift (stops at the first failure)
[working-directory: 'frontend']
fe-check:
    pnpm lint
    pnpm typecheck
    pnpm format:check
    pnpm check:api

# Frontend tests with coverage, then the production build
[working-directory: 'frontend']
fe-test:
    pnpm test
    pnpm build

# Vite dev server against `uv run delphi api` (CONTRIBUTING: "Sign in locally")
[working-directory: 'frontend']
fe-dev:
    pnpm dev

# The whole stack from the production image: api :8000, Vite :5173 (docs/23 §10)
dev:
    docker compose up --build

# Load the `test` dataset with the personas into the running stack
seed-test:
    docker compose exec api delphi seed --if-empty --profile test --personas

# Enable the repository's git hooks (commit authorship rule)
hooks:
    git config core.hooksPath .githooks

# Start the local PostgreSQL 16 (compose.yaml)
db:
    docker compose up -d postgres

# Apply migrations to $DELPHI_DATABASE_URL
[working-directory: 'backend']
migrate:
    uv run delphi migrate

# Serve the API on :8000 (needs DELPHI_DATABASE_URL)
[working-directory: 'backend']
api:
    uv run delphi api

# Run the worker (needs DELPHI_DATABASE_URL)
[working-directory: 'backend']
worker:
    uv run delphi worker
