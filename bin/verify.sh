#!/usr/bin/env bash
# The watched repository's checks before a push: verify.sh WORKTREE BASE.
# The checks are its verify hook, VERIFY_HOOK WORKTREE BASE, or with none its CI_COMMAND run in the
# worktree. E2E_GATE is the run the task's issue asks for, validated by baton.e2e_gate:
# GATE_HOOK WORKTREE ARGS runs it after green checks, and its last output line is the result the
# runner reports. Hooks live on this host, never in the repository, so a branch can't rewrite the
# checks that judge it. The Python runner supplies these variables and the tool environment: LOGS is
# the repository's log directory, RUNNER_LOG the shared runner.log and RUNNER_LOG_LABEL its [owner/name].
# Both hooks get VERIFY_ARTIFACTS, an empty directory beside this attempt's log, for test output
# worth keeping (Playwright's traces and screenshots): a later run in the same worktree would erase
# it there. It is removed when the attempt passes or the hooks leave it empty.
set -u
[[ $# == 2 && -d "$1" ]] || { echo 'usage: verify.sh WORKTREE BASE' >&2; exit 2; }
wt=$1 base=$2
logs=${LOGS:-"$HOME/.local/state/baton/logs"}
mkdir -p "$logs"
runner_log=${RUNNER_LOG:-"$logs/runner.log"}
label=${RUNNER_LOG_LABEL:+"$RUNNER_LOG_LABEL "}
tag=${VERIFY_TAG:-verify}
[[ "$tag" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'invalid VERIFY_TAG' >&2; exit 2; }
stamp=$(date +%F-%H%M%S)
file="$logs/$stamp-$tag.log"
export VERIFY_ARTIFACTS="$logs/$stamp-$tag-artifacts"
mkdir -p "$VERIFY_ARTIFACTS"
start=$(date +%s)
checks() {
  if [[ -n "${VERIFY_HOOK:-}" ]]; then
    "$VERIFY_HOOK" "$wt" "$base"
  elif [[ -n "${CI_COMMAND:-}" ]]; then
    (cd "$wt" && bash -c "$CI_COMMAND")
  else
    echo 'verify: neither VERIFY_HOOK nor CI_COMMAND is set'
    return 2
  fi
}
if checks > "$file" 2>&1; then
  rc=0
else
  rc=$?
fi
gate=()
[[ -z "${E2E_GATE:-}" ]] || read -ra gate <<<"$E2E_GATE"
gate_line=
if (( rc == 0 && ${#gate[@]} )); then
  if [[ -z "${GATE_HOOK:-}" ]]; then
    echo "verify: the issue asks for an E2E gate but the repository has no gate hook" >> "$file"
    rc=2
  else
    gate_log=$(mktemp)
    if "$GATE_HOOK" "$wt" "${gate[@]}" > "$gate_log" 2>&1; then
      result=$(tr -d '\r' < "$gate_log" | sed $'s/\033\\[[0-9;]*[A-Za-z]//g' | grep -v '^[[:space:]]*$' | tail -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
      gate_line="E2E gate passed: ${gate[*]} (${result:-no result in its output})"
    else
      rc=$?
    fi
    cat "$gate_log" >> "$file"
    rm -f "$gate_log"
  fi
fi
duration=$(( $(date +%s) - start ))
if (( rc == 0 )); then
  rm -rf "$VERIFY_ARTIFACTS"
else
  rmdir "$VERIFY_ARTIFACTS" 2>/dev/null || true
fi
printf '%s %sverify tag=%s rc=%s duration=%ss log=%s\n' "$(date '+%F %T')" "$label" "$tag" "$rc" "$duration" "$file" >> "$runner_log"
if (( rc != 0 )); then
  printf 'Verification failed (exit %s, %ss; full log: %s).\n' "$rc" "$duration" "$file" >&2
  [[ ! -d "$VERIFY_ARTIFACTS" ]] || printf 'Test output kept in %s\n' "$VERIFY_ARTIFACTS" >&2
  printf 'Last 200 lines:\n' >&2
  tail -n 200 "$file" >&2
else
  printf 'Verification passed (%ss; log: %s)\n' "$duration" "$file"
  [[ -z "$gate_line" ]] || printf '%s\n' "$gate_line"
fi
exit "$rc"
