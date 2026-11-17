#!/usr/bin/env bash
# Publish the local Musk/Rocket Lab SQLite feed to GitHub Pages.
#
# Replaces /workspace/musk-feed/trigger_pages_update.sh, which is not in this
# repo. That script checked out origin/main with reset --hard, then ran
# `sync.py --export`. A dirty docs/data tree aborted the checkout, and a
# Rocket Lab HTTP 404 made `set -e` skip the commit. This script does neither.
#
# It does not fetch @rocketlab or @elonmusk. The monitor's sync.py already
# wrote the canonical rows. This process only unions those rows with origin
# and exports paginated JSON.
#
# Install (do this before deleting the dirty checkout):
#   cp scripts/trigger_pages_update.sh scripts/publish_local_db.py /workspace/musk-feed/
#   chmod +x /workspace/musk-feed/trigger_pages_update.sh
# Auth, pick one. The token is never written into the remote URL or git config:
#   gh auth login
#   # or export GH_TOKEN / GITHUB_TOKEN / MUSK_X_FEED_GITHUB_TOKEN
# Optional:
#   MUSK_FEED_DB=/path/to/the.db   # base copy; its rows win on duplicate ids
#   MUSK_FEED_DIR=/workspace/musk-feed
#   MUSK_X_FEED_WORKDIR=/workspace/musk-x-feed
#   MUSK_X_FEED_PUBLISH_DIR=/workspace/musk-x-feed-publish
#
# Recovery after a successful push shows the new post on the site:
#   mv /workspace/musk-x-feed /workspace/musk-x-feed.dirty-YYYYMMDD
# Keep that directory. Do not git reset --hard it. If the new posts lived only
# in its musk_feed.db, point MUSK_FEED_DB at the archived file until the
# monitor's database path is confirmed.
#
# /workspace/musk-feed/sync.py is not tracked here. If a published page is still
# missing a post the monitor inserted, send a sanitized copy of that script
# (no tokens) so MUSK_FEED_DB can be pointed at the file it writes.

set -euo pipefail

REPO="${MUSK_X_FEED_REPO:-jianxma/musk-x-feed}"
REF="${MUSK_X_FEED_REF:-main}"
REMOTE="${MUSK_X_FEED_REMOTE:-https://github.com/${REPO}.git}"
WORKDIR="${MUSK_X_FEED_WORKDIR:-/workspace/musk-x-feed}"
FEED_DIR="${MUSK_FEED_DIR:-/workspace/musk-feed}"
PUBLISH_DIR="${MUSK_X_FEED_PUBLISH_DIR:-/workspace/musk-x-feed-publish}"
LOG="${MUSK_PAGES_BUILD_LOG:-/tmp/musk_pages_build.log}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PUBLISH_PY="${MUSK_PUBLISH_PY:-$SCRIPT_DIR/publish_local_db.py}"

if [[ ! -f "$PUBLISH_PY" ]]; then
  echo "ABORT: missing $PUBLISH_PY (install it beside this script)" >&2
  exit 1
fi

ASKPASS=""
TMP=$(mktemp -d)
cleanup() {
  rm -rf "$TMP"
  if [[ -n "$ASKPASS" ]]; then
    rm -f "$ASKPASS"
  fi
}
trap cleanup EXIT

clear_stored_token() {
  local dir="$1"
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" config --local --unset-all "http.https://github.com/.extraheader" >/dev/null 2>&1 || true
  fi
}

configure_auth() {
  local token="${MUSK_X_FEED_GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
  if [[ -n "$token" ]]; then
    export GH_TOKEN="$token"
    ASKPASS=$(mktemp)
    cat >"$ASKPASS" <<'EOF'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *) printf '%s\n' "${GH_TOKEN:-}" ;;
esac
EOF
    chmod 700 "$ASKPASS"
    export GIT_ASKPASS="$ASKPASS"
    export GIT_TERMINAL_PROMPT=0
    return 0
  fi
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    export GIT_TERMINAL_PROMPT=0
    return 0
  fi
  echo "SKIP: no gh auth and no GH_TOKEN, GITHUB_TOKEN, or MUSK_X_FEED_GITHUB_TOKEN" >&2
  exit 0
}

use_gh_helper() {
  local dir="$1"
  if [[ -n "${GH_TOKEN:-}" ]]; then
    return 0
  fi
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    git -C "$dir" config --local credential.helper ""
    git -C "$dir" config --local credential.helper '!gh auth git-credential'
  fi
}

collect_dbs() {
  BASE=""
  SOURCES=()
  if [[ -n "${MUSK_FEED_DB:-}" ]]; then
    if [[ ! -f "$MUSK_FEED_DB" ]]; then
      echo "ABORT: MUSK_FEED_DB does not exist: $MUSK_FEED_DB" >&2
      exit 1
    fi
    BASE="$MUSK_FEED_DB"
  elif [[ -f "$WORKDIR/musk_feed.db" ]]; then
    BASE="$WORKDIR/musk_feed.db"
  elif [[ -f "$FEED_DIR/musk_feed.db" ]]; then
    BASE="$FEED_DIR/musk_feed.db"
  else
    echo "ABORT: no feed database. Set MUSK_FEED_DB to the file the monitor sync writes." >&2
    exit 1
  fi
  if [[ -f "$WORKDIR/musk_feed.db" && "$WORKDIR/musk_feed.db" != "$BASE" ]]; then
    SOURCES+=("$WORKDIR/musk_feed.db")
  fi
  if [[ -f "$FEED_DIR/musk_feed.db" && "$FEED_DIR/musk_feed.db" != "$BASE" ]]; then
    SOURCES+=("$FEED_DIR/musk_feed.db")
  fi
  echo "BASE $BASE"
  if [[ ${#SOURCES[@]} -gt 0 ]]; then
    printf 'SOURCE %s\n' "${SOURCES[@]}"
  fi
}

prepare_publish_repo() {
  mkdir -p "$PUBLISH_DIR"
  PUBLISH_DIR=$(cd "$PUBLISH_DIR" && pwd)
  REPO_DIR="$PUBLISH_DIR/repo"
  WT="$PUBLISH_DIR/wt"
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone --depth 30 "$REMOTE" "$REPO_DIR"
  fi
  clear_stored_token "$REPO_DIR"
  git -C "$REPO_DIR" remote set-url origin "$REMOTE"
  if [[ "$REMOTE" == https://* || "$REMOTE" == http://* ]]; then
    use_gh_helper "$REPO_DIR"
  fi
  git -C "$REPO_DIR" fetch --depth 30 origin "$REF"
}

fresh_worktree() {
  if [[ -d "$WT" ]]; then
    git -C "$REPO_DIR" worktree remove "$WT" >/dev/null 2>&1 || true
    case "$WT" in
      "$PUBLISH_DIR"/*) rm -rf "$WT" ;;
      *)
        echo "ABORT: refusing to delete worktree outside $PUBLISH_DIR" >&2
        return 1
        ;;
    esac
  fi
  git -C "$REPO_DIR" worktree prune >/dev/null 2>&1 || true
  git -C "$REPO_DIR" worktree add --detach "$WT" "origin/${REF}"
}

publish_once() {
  set -euo pipefail
  prepare_publish_repo
  fresh_worktree || return 1
  local origin_db="$TMP/origin.db"
  cp -a "$WT/musk_feed.db" "$origin_db"

  local -a accounts=()
  local account
  while IFS= read -r account; do
    [[ -n "$account" ]] && accounts+=("$account")
  done < <(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from feed_core import synced_handles; print("\n".join(sorted(synced_handles())))' "$WT")
  if [[ ${#accounts[@]} -eq 0 ]]; then
    echo "ABORT: no synced accounts in $WT" >&2
    return 1
  fi

  local -a merge_args=(
    "$PUBLISH_PY" merge
    --base "$BASE"
    --dest "$WT/musk_feed.db"
    --source "$origin_db"
  )
  local src
  for src in "${SOURCES[@]}"; do
    merge_args+=(--source "$src")
  done
  if [[ -d "$WORKDIR/docs/data" ]]; then
    merge_args+=(--json-root "$WORKDIR/docs/data")
  fi
  for account in "${accounts[@]}"; do
    merge_args+=(--account "$account")
  done
  python3 "${merge_args[@]}"

  local -a check_args=(
    "$PUBLISH_PY" check
    --dest "$WT/musk_feed.db"
    --source "$BASE"
    --source "$origin_db"
  )
  for src in "${SOURCES[@]}"; do
    check_args+=(--source "$src")
  done
  if [[ -d "$WORKDIR/docs/data" ]]; then
    check_args+=(--json-root "$WORKDIR/docs/data")
  fi
  for account in "${accounts[@]}"; do
    check_args+=(--account "$account")
  done
  python3 "${check_args[@]}"

  : >"$LOG"
  set +e
  set +o pipefail
  MUSK_FEED_DB="$WT/musk_feed.db" python3 "$WT/export_pages.py" | tee "$LOG"
  local py_status=${PIPESTATUS[0]}
  set -o pipefail
  set -e
  if [[ "$py_status" -ne 0 ]]; then
    echo "ABORT: export failed; canonical checkout was not modified" >&2
    return 1
  fi

  local -a export_check=("$PUBLISH_PY" check-export --db "$WT/musk_feed.db" --data-root "$WT/docs/data")
  for account in "${accounts[@]}"; do
    export_check+=(--account "$account")
  done
  python3 "${export_check[@]}"

  if ! grep -q 'CONTENT_CHANGED=yes' "$LOG"; then
    echo "OK: no content change; skip push"
    return 0
  fi

  git -C "$WT" add -- musk_feed.db docs/data
  if git -C "$WT" diff --cached --quiet; then
    echo "OK: nothing staged; skip push"
    return 0
  fi
  git -C "$WT" -c user.name="musk-feed-bot" -c user.email="musk-feed-bot@users.noreply.github.com" \
    commit -m "chore: update Elon Musk X feed [skip ci]"
  if ! git -C "$WT" push origin "HEAD:${REF}"; then
    echo "PUSH_REJECTED; will rebuild from origin and try again" >&2
    return 10
  fi
  echo "OK: pushed Pages data to ${REPO}@${REF}"
  return 0
}

if [[ "$REMOTE" == https://* || "$REMOTE" == http://* ]]; then
  configure_auth
fi

clear_stored_token "$WORKDIR"
if [[ -d "$WORKDIR/.git" ]]; then
  echo "Canonical checkout is not checked out or reset: $WORKDIR"
fi

collect_dbs

attempt=1
while [[ "$attempt" -le 3 ]]; do
  set +e
  publish_once
  status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    exit 0
  fi
  if [[ "$status" -ne 10 ]]; then
    exit "$status"
  fi
  attempt=$((attempt + 1))
done
echo "ABORT: push rejected three times; canonical data was left in place" >&2
exit 1
