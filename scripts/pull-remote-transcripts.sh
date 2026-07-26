#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
export PYTHONDONTWRITEBYTECODE=1

REMOTE_WORKER=${PODSEARCH_REMOTE_WORKER:?Set PODSEARCH_REMOTE_WORKER to the MacBook SSH host}
REMOTE_REPO=${PODSEARCH_REMOTE_REPO:?Set PODSEARCH_REMOTE_REPO to the absolute MacBook repository path}
SITE_BUILD_INTERVAL=${PODSEARCH_SITE_BUILD_INTERVAL_SECONDS:-900}
if [[ "$REMOTE_REPO" != /* ]]; then
  echo "PODSEARCH_REMOTE_REPO must be an absolute path" >&2
  exit 1
fi
if ! [[ "$SITE_BUILD_INTERVAL" == <-> && "$SITE_BUILD_INTERVAL" -gt 0 ]]; then
  echo "PODSEARCH_SITE_BUILD_INTERVAL_SECONDS must be a positive integer" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "Missing required command: rsync" >&2
  exit 1
fi

mkdir -p var/run var/worker-inbox
LOCK_DIR="var/run/remote-pull.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  EXISTING_PID=$(<"$LOCK_DIR/pid" 2>/dev/null || true)
  if [[ "$EXISTING_PID" == <-> ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "Remote transcript pull is already running as PID $EXISTING_PID; skipping."
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Could not recover the stale remote transcript pull lock." >&2
    exit 1
  fi
fi
print -r -- "$$" > "$LOCK_DIR/pid"
cleanup_lock() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

rsync -az --remove-source-files \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
  --include='*.json' \
  --exclude='*' \
  "$REMOTE_WORKER:$REMOTE_REPO/var/worker-outbox/" \
  "var/worker-inbox/"

OUTPUT=$(python3 -m podsearch --config config.toml \
  import-transcript-results var/worker-inbox)
print -r -- "$OUTPUT"
IMPORTED=$(print -r -- "$OUTPUT" | awk -F= '$1 == "imported" {print $2}')
if [ "${IMPORTED:-0}" -gt 0 ] || [ -f var/run/site-build.pending ]; then
  python3 -m podsearch --config config.toml build-site \
    --if-stale-seconds "$SITE_BUILD_INTERVAL"
fi
