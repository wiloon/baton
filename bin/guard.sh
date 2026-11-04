#!/usr/bin/env bash
# PreToolUse hook for the unattended agents. Reads the tool call as JSON on
# stdin; prints a "deny" decision for anything that could deploy, tag, touch
# infrastructure, read secrets or leave the agent's worktree. Silence means
# "no opinion": the permission rules in settings.json then decide.

[[ "${BATON_AGENT:-}" == 1 ]] || exit 0

input=$(cat)
tool=$(jq -r '.tool_name // ""' <<<"$input")
WT="${BATON_AGENT_WORKTREE:-/nonexistent}"
REPO="${BATON_AGENT_REPO:-/nonexistent}"
ROLE="${BATON_AGENT_ROLE:-dev}"
LOG_DIR="${BATON_AGENT_LOGS:-$HOME/.local/state/baton/logs}"

deny() {
  printf '%s DENY %s: %s\n' "$(date '+%F %T')" "$tool" "$1" >> "$LOG_DIR/guard.log"
  jq -n --arg r "baton guard: $1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
  exit 0
}

case "$tool" in
  Edit|Write|NotebookEdit|MultiEdit)
    # Vendor markers belong to the runner; an agent writing one could
    # impersonate another vendor and approve its own work.
    jq -e '[.tool_input.content, .tool_input.new_string, .tool_input.new_source, (.tool_input.edits // [] | .[].new_string)]
      | map(strings) | any(test("by=(cursor|codex)"))' <<<"$input" >/dev/null && deny "vendor markers are reserved for the runner"
    p=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // ""' <<<"$input")
    p=$(realpath -m -- "$p")
    # The owner's principles change only when the owner asks for it, in an interactive session.
    [[ "$p" == */docs/agents/principles.md ]] && deny "docs/agents/principles.md changes only when the owner asks for it; don't draft entries"
    if [[ "$ROLE" == review || "$ROLE" == spec_review || "$ROLE" == scan ]]; then
      [[ "$p" == /tmp/* ]] || deny "the $ROLE role is read-only; write only under /tmp (got $p)"
    else
      [[ "$p" == "$WT"/* || "$p" == /tmp/* ]] || deny "writes are limited to the worktree $WT (got $p)"
    fi
    ;;
  Read)
    p=$(jq -r '.tool_input.file_path // ""' <<<"$input")
    p=$(realpath -m -- "$p")
    [[ "$p" == "$HOME/.config/baton/"* || "$p" == "$HOME/.ssh/"* || "$p" == "$HOME/.config/gh/"* ]] && deny "secrets are off limits"
    ;;
  Bash)
    c=$(jq -r '.tool_input.command // ""' <<<"$input")
    # Start of a command: line start, after ; & | ( or $( and an optional sudo/env.
    S='(^|[;&|(`]|\$\()[[:space:]]*(sudo[[:space:]]+|env[[:space:]]+)?'
    has() { printf '%s\n' "$c" | grep -qE -- "$1"; }

    has '\.config/baton|telegram\.env|\.config/gh|\.ssh/|TELEGRAM_' && deny "secrets are off limits"
    has 'by=(cursor|codex)' && deny "vendor markers are reserved for the runner"
    has "${S}(env|printenv|export)([[:space:]]|$)" && ! has "${S}env[[:space:]]+[A-Z_]+=" && deny "no dumping the environment"

    has "${S}git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+tag\b" && deny "no git tags (v* deploys production)"
    if has "${S}git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push\b"; then
      has '--tags|--mirror|--all|--delete|refs/tags|[[:space:]]-d([[:space:]]|$)' && deny "no pushing or deleting tags/refs"
      has '--force([[:space:]]|$)|--force-if-includes|[[:space:]]-f([[:space:]]|$)|[[:space:]]\+[^[:space:]]' && deny "no force push (use --force-with-lease on your own branch)"
      has '[[:space:]:](main|master)([[:space:]]|$)|:main\b|HEAD:main' && deny "no pushing to main"
      has '[[:space:]](v[0-9]|chrome-v)' && deny "no pushing release tags"
    fi
    has "${S}git[[:space:]]+-C[[:space:]]" && ! has "git[[:space:]]+-C[[:space:]]+\"?$WT" && deny "git -C outside the worktree"
    has "(--git-dir|--work-tree)" && deny "no --git-dir/--work-tree"
    has "${S}cd[[:space:]]+\"?$REPO/?\"?([[:space:];&|]|$)" && deny "stay out of the base clone"

    has "${S}gh[[:space:]]+(release|workflow|api|secret|variable|repo|auth|ruleset|cache|codespace|gpg-key|ssh-key)\b" && deny "gh subcommand not allowed for the agent"
    has "${S}gh[[:space:]]+(label[[:space:]]+delete|issue[[:space:]]+(delete|transfer)|pr[[:space:]]+(close|reopen))\b" && deny "destructive gh action"
    has "${S}gh[[:space:]]+pr[[:space:]]+merge\b" && deny "agents never merge; the runner does"
    if [[ "$ROLE" == review || "$ROLE" == spec_review || "$ROLE" == scan ]]; then
      has "${S}git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(commit|push|add|rm|mv|reset|restore|checkout|switch|rebase|merge|cherry-pick|stash|branch[[:space:]]+-[mMdD]|apply|am)\b" \
        && deny "the $ROLE role is read-only"
    fi
    if [[ "$ROLE" == review || "$ROLE" == fix || "$ROLE" == dev ]]; then
      has "${S}gh[[:space:]]+pr[[:space:]]+comment\b" && deny "the runner posts review/fix artifacts"
      has "${S}git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push\b" && deny "the runner pushes"
    fi
    if [[ "$ROLE" == dev ]]; then
      has "${S}gh[[:space:]]+(pr|issue)[[:space:]]+(create|edit|comment|close|reopen|ready|review|merge)\b" \
        && deny "the implementer has no GitHub write access; the runner opens the PR"
    fi
    if [[ "$ROLE" == review || "$ROLE" == spec_review ]]; then
      has "${S}gh[[:space:]]+(pr[[:space:]]+(create|edit|close|reopen|ready|review|merge)|issue[[:space:]]+(create|edit|comment|close|reopen))\b" \
        && deny "the reviewer has no GitHub write access"
    fi
    if [[ "$ROLE" == spec ]]; then
      has "${S}gh[[:space:]]+(pr|issue)[[:space:]]+(create|edit|comment|close|reopen|ready|review|merge|lock|unlock|pin|unpin|develop)\b" \
        && deny "the spec author has no GitHub write access; write the body and reply under /tmp"
      has "${S}git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(push|rebase|reset|cherry-pick|merge)\b" && deny "the spec author only commits; the runner pushes"
      has 'principles\.md' && has '(>|sed[[:space:]]+-i|tee|mv|cp|rm)' && deny "docs/agents/principles.md changes need the owner's approval"
    fi
    if [[ "$ROLE" == scan ]]; then
      has "${S}gh[[:space:]]+(pr|issue)[[:space:]]+(create|edit|comment|close|reopen|ready|review|merge|lock|unlock|pin|unpin|develop)\b" \
        && deny "the scanner has no GitHub write access; write the issue under /tmp"
    fi

    has "${S}(kubectl|helm|argocd|ssh|scp|rsync|nerdctl|docker|podman|tofu|terraform|aws|stripe|systemctl|crontab|wg|wg-quick|ip)([[:space:]]|$)" && deny "infrastructure / host tool not allowed"
    has "${S}ko[[:space:]]+(build|publish|apply|resolve)\b" && deny "no image builds/pushes"
    has "${S}task[[:space:]][^;&|]*(deploy|release|publish|prod)" && deny "no deploy/release tasks"
    has "${S}rm[[:space:]]+(-[a-zA-Z]*[[:space:]]+)*(/|~|\\\$HOME|\.\.)([[:space:]/]|$)" && deny "rm outside the worktree"
    ;;
esac
exit 0
