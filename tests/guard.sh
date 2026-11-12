#!/usr/bin/env bash
# Offline checks of what stays in bash: guard.sh decisions, the prompts' shell rule and the
# settings files. The runner itself is Python (tests/test_*.py).
set -eu
BIN=$(cd "$(dirname "$0")/../bin" && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
STATE="$TEST_ROOT/state"; mkdir -p "$STATE"
fail() { echo "FAIL: $*" >&2; exit 1; }
for vendor in cursor codex; do
  decision=$(printf '{"tool_name":"Write","tool_input":{"content":"by=%s"}}' "$vendor" \
    | BATON_AGENT=1 BATON_AGENT_LOGS="$STATE" bash "$BIN/guard.sh")
  [[ "$(jq -r '.hookSpecificOutput.permissionDecision' <<<"$decision")" == deny ]] || fail "guard $vendor"
done
# Implementers commit but never push or write to GitHub; a spec reviewer is read-only.
guard_decision() {   # ROLE TOOL_JSON -> deny, or none when the guard stays silent
  local d
  d=$(printf '%s' "$2" | BATON_AGENT=1 BATON_AGENT_ROLE=$1 BATON_AGENT_WORKTREE="$TEST_ROOT" BATON_AGENT_LOGS="$STATE" bash "$BIN/guard.sh" \
    | jq -r '.hookSpecificOutput.permissionDecision')
  echo "${d:-none}"
}
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"git push origin HEAD"}}') == deny ]] || fail guard-dev-push
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"gh pr create --fill"}}') == deny ]] || fail guard-dev-pr
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}') == none ]] || fail guard-dev-commit
[[ $(guard_decision spec_review '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}') == deny ]] || fail guard-spec-review-commit
git_c_commit=$(jq -nc --arg cmd "git -C $TEST_ROOT commit -m x" '{tool_name:"Bash",tool_input:{command:$cmd}}')
git_c_add=$(jq -nc --arg cmd "git -C $TEST_ROOT add file" '{tool_name:"Bash",tool_input:{command:$cmd}}')
git_c_push=$(jq -nc --arg cmd "git -C $TEST_ROOT push" '{tool_name:"Bash",tool_input:{command:$cmd}}')
[[ $(guard_decision spec "$git_c_commit") == none ]] || fail guard-spec-git-c-commit
[[ $(guard_decision spec "$git_c_add") == none ]] || fail guard-spec-git-c-add
[[ $(guard_decision spec "$git_c_push") == deny ]] || fail guard-spec-git-c-push
[[ $(guard_decision spec_review "$git_c_commit") == deny ]] || fail guard-spec-review-git-c-commit
# Check the settings patterns as well as the guard: silence from the hook alone
# does not allow a command in Claude's dontAsk mode.
settings_match() { # allow|deny COMMAND
  local rule pattern
  while IFS= read -r rule; do
    pattern=${rule#Bash(}; pattern=${pattern%)}
    [[ $2 == $pattern ]] && return 0
  done < <(jq -r --arg kind "$1" '.permissions[$kind][] | select(startswith("Bash("))' "$BIN/spec-settings.json")
  return 1
}
for cmd in "git add file" "git commit -m x" "git -C $TEST_ROOT add file" "git -C $TEST_ROOT commit -m x"; do
  settings_match allow "$cmd" || fail "spec settings do not allow $cmd"
  ! settings_match deny "$cmd" || fail "spec settings deny $cmd"
done
settings_match deny "git -C $TEST_ROOT push" || fail spec-settings-git-c-push
! settings_match allow "git -C $TEST_ROOT push" || fail spec-settings-allow-git-c-push
[[ $(guard_decision spec_review "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$TEST_ROOT/x\",\"content\":\"a\"}}") == deny ]] || fail guard-spec-review-write
[[ $(guard_decision spec_review '{"tool_name":"Bash","tool_input":{"command":"gh issue comment 1 --body x"}}') == deny ]] || fail guard-spec-review-comment
# Claude runs in dontAsk mode: a compound command (`cd x; F=y; wc $F`) is denied
# even when each part is allowed. Every prompt Claude can get must say so.
for prompt in dev fix review scan spec spec-review; do
  grep -q 'one command per call' "$BIN/$prompt-prompt.md" || fail "shell rule missing in $prompt-prompt.md"
done
# Every Claude call pays for the skills, tools and connectors it is shown. Unattended
# runs keep the Matt skills and the coding tools, and see nothing else.
for s in settings review-settings spec-settings scan-settings; do
  jq -e '.disableClaudeAiConnectors == true and .disableBundledSkills == true
    and .skillOverrides["anthropic-skills:docx"] == "off" and .skillOverrides.omarchy == "off"
    and ([.skillOverrides | keys[] | select(startswith("mattpocock-skills"))] | length == 0)
    and (.permissions.deny | index("ScheduleWakeup") and index("RemoteTrigger") and index("CronCreate"))' \
    "$BIN/$s.json" >/dev/null || fail "$s.json does not trim the unattended context"
done
echo 'PASS: guard decisions, prompt shell rule, unattended settings'
