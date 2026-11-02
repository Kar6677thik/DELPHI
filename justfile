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

# Enable the repository's git hooks (commit authorship rule)
hooks:
    git config core.hooksPath .githooks
