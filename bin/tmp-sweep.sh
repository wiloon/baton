#!/usr/bin/env bash
# Keep the agent's files from filling /tmp (#248): a tmpfs with a per-user quota fills up,
# and then every run and deploy fails with "Disk quota exceeded". systemd-tmpfiles ages
# /tmp only after 10 days.
#   tmp-sweep.sh N...  remove what finished issue/PR N left: the runner's own files
#                      (baton-dev-N.pr.md, baton-review-N-R.md, ...) and the test databases,
#                      caches and binaries agents name after the task (PREFIX-N-*, PREFIX-prN-*)
#   tmp-sweep.sh       (baton-tmp-sweep.timer) remove the agent's entries with
#                      nothing written in the last day; jest's shared cache is trimmed
#                      per entry
# Only this user's top-level entries matching the names below are touched.
set -u
TMP_ROOT=${BATON_TMP_ROOT:-/tmp}
STALE_MINUTES=${BATON_TMP_STALE_MINUTES:-1440}
me=$(id -un)

# Agent-made names: task artifacts, cgo and go-build scratch, and the Node caches that
# lived here before env.sh moved them to $HOME.
# Name prefixes: the runner's own (baton) and the watched repositories' (BATON_TMP_PREFIXES,
# space-separated, from config.toml repos.tmp_prefixes). Anything but a plain name is ignored.
PREFIXES=(baton)
read -r -a extra <<< "${BATON_TMP_PREFIXES:-}"
# Bash 3.2 treats an empty array as unset under nounset.
for prefix in ${extra[@]+"${extra[@]}"}; do
  [[ "$prefix" =~ ^[A-Za-z0-9][A-Za-z0-9_.]*$ ]] && PREFIXES+=("$prefix")
done
STALE_NAMES=('cgo-gcc-input-*' 'go-build*' .pnpm-store node-compile-cache)
for prefix in "${PREFIXES[@]}"; do STALE_NAMES+=("$prefix-*"); done

remove() { rm -rf -- "$@" 2>/dev/null; }

# stale PATH: true when nothing under PATH (or PATH itself) changed in STALE_MINUTES.
stale() {
  [[ -z "$(find "$1" -mmin "-$STALE_MINUTES" -print -quit 2>/dev/null)" ]]
}

sweep_task() {
  local n p names
  for n; do
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    names=()
    for p in "${PREFIXES[@]}"; do
      names+=(-o -name "$p-$n-*" -o -name "$p-pr$n-*" -o -name "$p-dev-$n.*" -o -name "$p-dev-$n-*"
              -o -name "$p-review-$n-*" -o -name "$p-fix-$n.md*" -o -name "$p-spec-$n-*")
    done
    find "$TMP_ROOT" -mindepth 1 -maxdepth 1 -user "$me" \( "${names[@]:1}" \) \
      -exec rm -rf -- {} + 2>/dev/null
  done
}

sweep_stale() {
  local name p
  local args=()
  for name in "${STALE_NAMES[@]}"; do args+=(-o -name "$name"); done
  while IFS= read -r -d '' p; do
    stale "$p" && remove "$p"
  done < <(find "$TMP_ROOT" -mindepth 1 -maxdepth 1 -user "$me" \( "${args[@]:1}" \) -print0 2>/dev/null)
  # Jest keeps one cache dir for every checkout (jest_<uid in base 36>); each worktree
  # adds its own haste maps.
  for p in "$TMP_ROOT"/jest_*/*; do
    [[ -e "$p" && -O "$p" ]] && stale "$p" && remove "$p"
  done
}

if (( $# )); then sweep_task "$@"; else sweep_stale; fi
exit 0
