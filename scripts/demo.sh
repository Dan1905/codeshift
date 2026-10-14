#!/usr/bin/env bash
# Walk through the full CodeShift workflow on a throwaway copy of the demo service.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
CS="$ROOT/.venv/bin/codeshift"
[ -x "$CS" ] || CS="codeshift"
# Load .env (e.g. GROQ_API_KEY) if present. DEMO_LLM picks the provider: sample (default) or groq.
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
LLM="${DEMO_LLM:-sample}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/codeshift-demo.XXXXXX")"
export CODESHIFT_HOME="$WORK/state"
cp -R "$ROOT/examples/legacy_service" "$WORK/legacy_service"
find "$WORK" -name __pycache__ -prune -exec rm -rf {} +
cd "$WORK"
git -C legacy_service init -q
git -C legacy_service add -A
git -C legacy_service -c user.email=demo@example.com -c user.name=demo commit -qm "legacy service"

step() {
  printf '\n\033[1;36m==> %s\033[0m\n' "$1"
  printf '\033[2m$ %s\033[0m\n\n' "$2"
  sleep 1
}

step "1. Scan the repository (read-only)" "codeshift scan ./legacy_service"
"$CS" scan ./legacy_service

step "2. Plan the migration (sync-to-async + deprecated APIs)" "codeshift plan ./legacy_service --llm $LLM"
"$CS" plan ./legacy_service --llm "$LLM"

step "3. Review the proposed diff" "codeshift diff"
"$CS" diff

step "4. Applying without approval is refused" "codeshift apply --non-interactive"
"$CS" apply --non-interactive
echo "exit code: $?"

step "5. Apply with explicit approval" "codeshift apply --yes"
"$CS" apply --yes

step "6. Verify: syntax, structure, ruff, tests vs baseline, review" "codeshift verify --run-tests"
"$CS" verify --run-tests
echo "exit code: $?"

step "7. Machine-readable report (excerpt)" "codeshift report --format json"
"$CS" report --format json 2>/dev/null | head -25

step "8. Rollback refuses after a later manual edit" "echo '# my edit' >> app/main.py && codeshift rollback"
cp legacy_service/app/main.py "$WORK/main.py.bak"
echo "# my edit" >> legacy_service/app/main.py
"$CS" rollback
echo "exit code: $?"
cp "$WORK/main.py.bak" legacy_service/app/main.py

step "9. Rollback once the edit is reverted" "codeshift rollback"
"$CS" rollback
printf 'git status after rollback: '
[ -z "$(git -C legacy_service status --porcelain)" ] && echo "clean (all files restored)" || git -C legacy_service status --short

step "10. Run history" "codeshift history"
"$CS" history

printf '\n\033[1;32mDemo finished.\033[0m Workspace: %s\n' "$WORK"
