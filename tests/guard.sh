#!/usr/bin/env bash
# Offline checks of what stays in bash: guard.sh decisions, the prompts' shell rule and the
# settings files. The runner itself is Python (tests/test_*.py).
set -eu
BIN=$(cd "$(dirname "$0")/../bin" && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
STATE="$TEST_ROOT/state"; mkdir -p "$STATE"
# The agent worktree as guard.sh sees it. It need not exist, and it must not sit under /tmp
# or behind a symlink (macOS mktemp: /var -> /private/var).
WT=/nonexistent/baton-guard-wt
# The run's scratch directory (#306): <state>/repos/<owner>/<repo>/scratch.
REPOS="$TEST_ROOT/root/repos"; SCRATCH="$REPOS/owner/app/scratch"; mkdir -p "$SCRATCH"
fail() { echo "FAIL: $*" >&2; exit 1; }
for vendor in cursor codex; do
  decision=$(printf '{"tool_name":"Write","tool_input":{"content":"by=%s"}}' "$vendor" \
    | BATON_AGENT=1 BATON_AGENT_LOGS="$STATE" bash "$BIN/guard.sh")
  [[ "$(jq -r '.hookSpecificOutput.permissionDecision' <<<"$decision")" == deny ]] || fail "guard $vendor"
done
# Implementers commit but never push or write to GitHub; a spec reviewer is read-only.
guard_decision() {   # ROLE TOOL_JSON -> deny, or none when the guard stays silent
  local d
  d=$(printf '%s' "$2" | BATON_AGENT=1 BATON_AGENT_ROLE=$1 BATON_AGENT_WORKTREE="$WT" BATON_AGENT_LOGS="$STATE" \
    BATON_AGENT_SCRATCH="${GUARD_SCRATCH-$SCRATCH}" bash "$BIN/guard.sh" | jq -r '.hookSpecificOutput.permissionDecision')
  echo "${d:-none}"
}
# #306: each role writes its own repository's scratch directory, never /tmp, another
# repository's scratch or the runner's records next to its own.
write_to() {   # TOOL PATH -> tool JSON
  jq -nc --arg t "$1" --arg p "$2" '{tool_name:$t,tool_input:{file_path:$p,content:"a",old_string:"a",new_string:"b"}}'
}
expect() {   # WANT ROLE PATH
  local tool got
  for tool in Write Edit; do
    got=$(guard_decision "$2" "$(write_to "$tool" "$3")")
    [[ $got == "$1" ]] || fail "guard-scratch $tool $2 $3: want $1, got $got"
  done
}
mkdir -p "$REPOS/other/shop/scratch"
ln -s "$TEST_ROOT/root" "$TEST_ROOT/linked-root"
for role in dev fix review screenshot_review spec_review spec scan; do
  expect none "$role" "$SCRATCH/out.md"
  for f in "$REPOS/other/shop/scratch/out.md" /tmp/baton-out.md "$REPOS/owner/app/lock" "$REPOS/owner/app/scratch-x" \
    "$SCRATCH/../lock"; do
    expect deny "$role" "$f"
  done
  case "$role" in
    dev|fix|spec) expect none "$role" "$WT/x" ;;
    *) expect deny "$role" "$WT/x" ;;
  esac
  GUARD_SCRATCH="$TEST_ROOT/linked-root/repos/owner/app/scratch"
  expect none "$role" "$GUARD_SCRATCH/out.md"
  expect none "$role" "$SCRATCH/out.md"
  # Unset or empty: no extra path, and never a match on /*.
  GUARD_SCRATCH=
  for f in "$SCRATCH/out.md" /tmp/baton-out.md /etc/x /x; do expect deny "$role" "$f"; done
  unset GUARD_SCRATCH
  decision=$(write_to Write /etc/x | env -u BATON_AGENT_SCRATCH BATON_AGENT=1 BATON_AGENT_ROLE="$role" \
    BATON_AGENT_WORKTREE="$WT" BATON_AGENT_LOGS="$STATE" bash "$BIN/guard.sh" | jq -r '.hookSpecificOutput.permissionDecision')
  [[ $decision == deny ]] || fail "guard-scratch unset $role"
done
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"git push origin HEAD"}}') == deny ]] || fail guard-dev-push
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"gh pr create --fill"}}') == deny ]] || fail guard-dev-pr
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}') == none ]] || fail guard-dev-commit
[[ $(guard_decision spec_review '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}') == deny ]] || fail guard-spec-review-commit
git_c_commit=$(jq -nc --arg cmd "git -C $WT commit -m x" '{tool_name:"Bash",tool_input:{command:$cmd}}')
git_c_add=$(jq -nc --arg cmd "git -C $WT add file" '{tool_name:"Bash",tool_input:{command:$cmd}}')
git_c_push=$(jq -nc --arg cmd "git -C $WT push" '{tool_name:"Bash",tool_input:{command:$cmd}}')
[[ $(guard_decision spec "$git_c_commit") == none ]] || fail guard-spec-git-c-commit
[[ $(guard_decision spec "$git_c_add") == none ]] || fail guard-spec-git-c-add
[[ $(guard_decision spec "$git_c_push") == deny ]] || fail guard-spec-git-c-push
[[ $(guard_decision spec_review "$git_c_commit") == deny ]] || fail guard-spec-review-git-c-commit
# #84: test files in the worktree may hold vendor markers as fixtures; anything the runner
# could post (scratch files) and non-test code still may not.
marker_write() {   # PATH -> Write tool JSON whose content holds a vendor marker
  jq -nc --arg p "$1" '{tool_name:"Write",tool_input:{file_path:$p,content:"<!-- baton-fix by=codex -->"}}'
}
marker_edit() {
  jq -nc --arg p "$1" '{tool_name:"Edit",tool_input:{file_path:$p,old_string:"a",new_string:"by=cursor"}}'
}
for f in tests/test_review.py pkg/review_test.go src/review.test.ts web/__tests__/x.js test/x.py; do
  [[ $(guard_decision dev "$(marker_write "$WT/$f")") == none ]] || fail "guard-marker-test-file $f"
  [[ $(guard_decision fix "$(marker_edit "$WT/$f")") == none ]] || fail "guard-marker-test-edit $f"
done
for f in "$WT/baton/review.py" "$WT/README.md" "$SCRATCH/reply.md" "$SCRATCH/tests/reply.md" "$WT/tests/../baton/x.py"; do
  [[ $(guard_decision dev "$(marker_write "$f")") == deny ]] || fail "guard-marker-non-test $f"
done
[[ $(guard_decision dev '{"tool_name":"Bash","tool_input":{"command":"echo by=codex > tests/x"}}') == deny ]] || fail guard-marker-bash
# #75: the fixer edits the description through a scratch file; create/edit go through the runner.
[[ $(guard_decision fix '{"tool_name":"Bash","tool_input":{"command":"gh pr create --fill"}}') == deny ]] || fail guard-fix-pr-create
[[ $(guard_decision fix '{"tool_name":"Bash","tool_input":{"command":"gh pr edit 1 --body x"}}') == deny ]] || fail guard-fix-pr-edit
[[ $(guard_decision fix '{"tool_name":"Bash","tool_input":{"command":"gh pr view 1"}}') == none ]] || fail guard-fix-pr-view
[[ $(guard_decision fix "$(write_to Write "$SCRATCH/fix-1.md.body")") == none ]] || fail guard-fix-body-artifact
# #84: the dev/fix settings run each repo's own checks, Python included.
dev_allows() {
  local rule pattern
  while IFS= read -r rule; do
    pattern=${rule#Bash(}; pattern=${pattern%)}
    [[ $1 == $pattern ]] && return 0
  done < <(jq -r '.permissions.allow[] | select(startswith("Bash("))' "$BIN/settings.json")
  return 1
}
for cmd in "uv run --frozen pytest -q" "python3 -m pytest -q" "pytest -q tests/test_review.py"; do
  dev_allows "$cmd" || fail "dev settings do not allow $cmd"
done
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
for cmd in "git add file" "git commit -m x" "git -C $WT add file" "git -C $WT commit -m x"; do
  settings_match allow "$cmd" || fail "spec settings do not allow $cmd"
  ! settings_match deny "$cmd" || fail "spec settings deny $cmd"
done
settings_match deny "git -C $WT push" || fail spec-settings-git-c-push
! settings_match allow "git -C $WT push" || fail spec-settings-allow-git-c-push
[[ $(guard_decision spec_review "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$WT/x\",\"content\":\"a\"}}") == deny ]] || fail guard-spec-review-write
[[ $(guard_decision spec_review '{"tool_name":"Bash","tool_input":{"command":"gh issue comment 1 --body x"}}') == deny ]] || fail guard-spec-review-comment
# The screenshot reviewer is read-only like the code reviewer; the runner publishes its comment.
[[ $(guard_decision screenshot_review '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}') == deny ]] || fail guard-screenshot-review-commit
[[ $(guard_decision screenshot_review '{"tool_name":"Bash","tool_input":{"command":"git push origin HEAD"}}') == deny ]] || fail guard-screenshot-review-push
[[ $(guard_decision screenshot_review '{"tool_name":"Bash","tool_input":{"command":"gh pr comment 1 --body x"}}') == deny ]] || fail guard-screenshot-review-comment
[[ $(guard_decision screenshot_review '{"tool_name":"Bash","tool_input":{"command":"gh pr edit 1 --body x"}}') == deny ]] || fail guard-screenshot-review-edit
[[ $(guard_decision screenshot_review "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$TEST_ROOT/x\",\"content\":\"a\"}}") == deny ]] || fail guard-screenshot-review-write
[[ $(guard_decision screenshot_review "$(write_to Write "$SCRATCH/screenshot-review-1-1.md")") == none ]] || fail guard-screenshot-review-artifact
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
