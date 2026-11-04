#!/usr/bin/env bash
# Offline verification gate: successful checks, bounded repair, and CI classification.
set -u
BIN=$(cd "$(dirname "$0")/../bin" && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
export TEST_ROOT HOME="$TEST_ROOT/home" PATH="$TEST_ROOT/stubs:$PATH"
mkdir -p "$HOME" "$TEST_ROOT/stubs" "$TEST_ROOT/wt" "$TEST_ROOT/logs" "$TEST_ROOT/state" "$TEST_ROOT/ctx"
fail() { echo "FAIL: $*" >&2; exit 1; }
cat > "$TEST_ROOT/stubs/task" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_ROOT/task-calls"
[[ ! -f "$TEST_ROOT/task-fail" ]] || { echo 'golangci-lint: unused helper' >&2; exit 4; }
echo 'no tasks selected or all checks passed'
STUB
chmod +x "$TEST_ROOT/stubs/task"

CI_COMMAND="task ci:changed BASE=origin/main" LOGS="$TEST_ROOT/logs" VERIFY_TAG=issue-1-verify-0 "$BIN/verify.sh" "$TEST_ROOT/wt" origin/main >/dev/null || fail 'passing checks failed'
grep -qx 'ci:changed BASE=origin/main' "$TEST_ROOT/task-calls" || fail 'wrong CI command or BASE'
touch "$TEST_ROOT/task-fail"
if CI_COMMAND="task ci:changed BASE=origin/main" LOGS="$TEST_ROOT/logs" VERIFY_TAG=issue-1-verify-1 "$BIN/verify.sh" "$TEST_ROOT/wt" origin/main > "$TEST_ROOT/error" 2>&1; then
  fail 'failing checks passed'
fi
grep -q 'golangci-lint: unused helper' "$TEST_ROOT/error" || fail 'failure output missing'
grep -q 'duration=' "$TEST_ROOT/logs/runner.log" || fail 'duration not logged'

# The CI repair round (classification, one repair per head, hand-off when it cannot run): tests/test_merge.py.
# Fix runs and the verification repair loop: tests/test_fix.py.
echo 'PASS: verification and CI repair classification'
