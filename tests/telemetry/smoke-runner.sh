#!/usr/bin/env bash
# Explicit opt-in: one real, isolated Codex invocation; no GitHub operations.
# Usage: bash smoke-runner.sh /path/to/candidate/baton/bin
set -eu
SELF_DIR=$(cd "${1:?candidate bin directory required}" && pwd)
ROOT=$(mktemp -d /tmp/baton-runner-smoke.XXXXXX)
# Keep local diagnostics private. Output only bounded metadata; retain for readback.
chmod 700 "$ROOT"
set -a
source "$SELF_DIR/config.env"
source "$SELF_DIR/env.sh"
set +a
export SELF_DIR STATE="$ROOT/state" LOGS="$ROOT/logs" MANUAL=smoke ISSUE='' PR='' PYTHONPATH="${SELF_DIR%/*}"
mkdir -p "$STATE" "$LOGS" "$ROOT/repo"
git -C "$ROOT/repo" init -q
git -C "$ROOT/repo" -c user.name=Telemetry -c user.email=telemetry@example.invalid -c core.hooksPath=/dev/null commit --allow-empty -qm smoke
export CODEX_MAX_MINUTES=2
printf '%s' 'Do not run tools or read files. Reply with the sentence "Telemetry smoke completed." followed by a final line exactly "REVIEW: pass".' \
  | uv run --quiet --frozen --no-dev --project "${SELF_DIR%/*}" python -m baton.dispatch --vendor codex --role review --tag smoke \
    --cwd "$ROOT/repo" --output "$ROOT/review.md" >/dev/null
python3 - "$STATE" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
records = list((root / 'telemetry/runs').glob('*.json'))
assert len(records) == 1, 'expected one real CLI record'
record = json.loads(records[0].read_text())
assert record['status'] == 'success'
assert record['usage_state'] == 'complete'
print(json.dumps({k: record[k] for k in ('run_id','trace_id','span_id','agent','status','usage_state','input_tokens','output_tokens','cache_read_tokens')}, indent=2))
print('isolated_state=' + str(root))
PY
