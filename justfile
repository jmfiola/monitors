set shell := ["bash", "-euo", "pipefail", "-c"]

_default:
    @just --list

# Install every workspace member and development dependency.
setup:
    uv sync

# Run the test suite, optionally forwarding pytest arguments.
test *args:
    uv run pytest -q {{ args }}

# Run strict static type checking across the workspace.
typecheck:
    uv run mypy --strict lib apps tests tools

# Check Python lint rules.
lint:
    uv run ruff check .

# Check Python formatting without changing files.
format-check:
    uv run ruff format --check .

# Format Python files.
format:
    uv run ruff format .

# Run the standard local validation suite.
check: test typecheck lint format-check

# Compare Melanzana and Jeffco output with their TypeScript implementations.
parity:
    ./tools/parity-diff.sh

# Preview production infrastructure changes without applying them.
infra-plan:
    ./infra/deploy.sh --plan

# Verify the production host without changing it.
infra-verify:
    ./infra/deploy.sh --verify

# Read one app's production logs, forwarding gcloud logging arguments.
logs app *args:
    ./infra/logs.sh {{ app }} {{ args }}
