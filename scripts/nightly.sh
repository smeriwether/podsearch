#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
mkdir -p var/logs var/run

LOCK_DIR="var/run/pipeline.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -f "$LOCK_DIR/pid" ]; then
    LOCK_PID=$(<"$LOCK_DIR/pid")
    if [[ "$LOCK_PID" == <-> ]] && kill -0 "$LOCK_PID" 2>/dev/null; then
      echo "podsearch nightly run already in progress; skipping"
      exit 0
    fi
  fi
  rm -f "$LOCK_DIR/pid" "$LOCK_DIR/started_at"
  rmdir "$LOCK_DIR" 2>/dev/null || {
    echo "podsearch lock exists and could not be cleared: $LOCK_DIR" >&2
    exit 1
  }
  mkdir "$LOCK_DIR"
fi

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$LOCK_DIR/started_at"
print -r -- "$$" > "$LOCK_DIR/pid"
cleanup_lock() {
  rm -f "$LOCK_DIR/pid" "$LOCK_DIR/started_at"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

export PYTHONDONTWRITEBYTECODE=1
BACKFILL_PID_FILE="var/run/backfill.lock/pid"
if [ -f "$BACKFILL_PID_FILE" ]; then
  BACKFILL_PID=$(<"$BACKFILL_PID_FILE")
else
  BACKFILL_PID=""
fi

if [[ "$BACKFILL_PID" == <-> ]] && kill -0 "$BACKFILL_PID" 2>/dev/null; then
  # Keep discovering and saving new episodes every day while the dedicated
  # backfill worker owns transcription.
  python3 -m podsearch --config config.toml run-nightly --metadata-only
else
  python3 -m podsearch --config config.toml run-nightly
fi
