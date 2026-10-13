#!/usr/bin/env bash
# Run the same checks as CI: format, lint, types, tests with coverage, package build.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
echo "== ruff format =="
"$PY" -m ruff format --check src tests
echo "== ruff check =="
"$PY" -m ruff check src tests
echo "== mypy =="
"$PY" -m mypy
echo "== pytest =="
"$PY" -m pytest --cov=codeshift --cov-report=term-missing:skip-covered
echo "All checks passed."
