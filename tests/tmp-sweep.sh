#!/usr/bin/env bash
# Offline tests for bin/tmp-sweep.sh (#248): per-task removal and the stale sweep.
set -eu
BIN=$(cd "$(dirname "$0")/../bin" && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
export BATON_TMP_ROOT="$TEST_ROOT/tmp"
mkdir -p "$BATON_TMP_ROOT"
# A repository's own prefix (config.toml repos.tmp_prefixes); the runner's, baton, is built in.
export BATON_TMP_PREFIXES="app"
fail() { echo "FAIL: $*" >&2; exit 1; }
gone() { [[ ! -e "$BATON_TMP_ROOT/$1" ]] || fail "$1 should be removed"; }
kept() { [[ -e "$BATON_TMP_ROOT/$1" ]] || fail "$1 should be kept"; }
old() { touch -t 202601010000 "$@"; }

# --- per task --------------------------------------------------------------------
cd "$BATON_TMP_ROOT"
mkdir app-45-lint-cache app-dev-45-shots app-450-lint-cache app-4-api.d baton-45-lint-cache
touch app-45-integration.db app-pr45-unit.log app-dev-45.pr.md app-review-45-1.md \
  app-fix-45.md app-spec-45-2.body app-review-450-1.md app-pr4-unit.log other-45-x \
  baton-dev-45.pr.md baton-review-45-1.md baton-fix-45.md baton-spec-45-2.body baton-review-450-1.md
"$BIN/tmp-sweep.sh" 45 not-a-number
for f in app-45-lint-cache app-dev-45-shots app-45-integration.db app-pr45-unit.log \
  app-dev-45.pr.md app-review-45-1.md app-fix-45.md app-spec-45-2.body \
  baton-45-lint-cache baton-dev-45.pr.md baton-review-45-1.md baton-fix-45.md baton-spec-45-2.body; do gone "$f"; done
for f in app-450-lint-cache app-4-api.d app-review-450-1.md app-pr4-unit.log other-45-x baton-review-450-1.md; do kept "$f"; done
rm -rf "${BATON_TMP_ROOT:?}"/* "$BATON_TMP_ROOT"/.[!.]* 2>/dev/null || true

# --- stale sweep -----------------------------------------------------------------
mkdir -p app-1-lint-cache/ab app-2-lint-cache/cd .pnpm-store/v11 node-compile-cache jest_rs/old jest_rs/new go-build123
touch baton-scan-old.body app-old.db app-new.db cgo-gcc-input-1.c app-1-lint-cache/ab/x app-2-lint-cache/cd/x \
  .pnpm-store/v11/index.db jest_rs/old/m jest_rs/new/m unrelated.log
old baton-scan-old.body app-old.db cgo-gcc-input-1.c app-1-lint-cache app-1-lint-cache/ab app-1-lint-cache/ab/x \
  app-2-lint-cache app-2-lint-cache/cd .pnpm-store .pnpm-store/v11 .pnpm-store/v11/index.db \
  node-compile-cache jest_rs jest_rs/old jest_rs/old/m jest_rs/new go-build123 unrelated.log
# From a directory where the patterns would match (the runner's $HOME has baton/).
mkdir "$TEST_ROOT/cwd" && touch "$TEST_ROOT/cwd/baton-x" "$TEST_ROOT/cwd/app-x"
(cd "$TEST_ROOT/cwd" && "$BIN/tmp-sweep.sh")
for f in baton-scan-old.body app-old.db cgo-gcc-input-1.c app-1-lint-cache .pnpm-store node-compile-cache jest_rs/old go-build123; do gone "$f"; done
# app-2-lint-cache/cd/x was written just now, so the whole cache stays.
for f in app-new.db app-2-lint-cache jest_rs jest_rs/new unrelated.log; do kept "$f"; done

rm -rf "${BATON_TMP_ROOT:?}"/* "$BATON_TMP_ROOT"/.[!.]* 2>/dev/null || true

# --- prefixes ----------------------------------------------------------------------
# Without the variable only the runner's own names go; a prefix that isn't a plain name is ignored.
touch baton-fix-46.md app-46-x.db shop-46-x.db shop   # "shop": what an unquoted * would expand to
BATON_TMP_PREFIXES= "$BIN/tmp-sweep.sh" 46
gone baton-fix-46.md; kept app-46-x.db; kept shop-46-x.db
BATON_TMP_PREFIXES="* app" "$BIN/tmp-sweep.sh" 46
gone app-46-x.db; kept shop-46-x.db

echo "tmp-sweep: ok"
