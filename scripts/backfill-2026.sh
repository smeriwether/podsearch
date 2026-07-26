#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
export PYTHONDONTWRITEBYTECODE=1

BATCH_SIZE=${PODSEARCH_BACKFILL_BATCH_SIZE:-2}
SITE_BUILD_INTERVAL=${PODSEARCH_SITE_BUILD_INTERVAL_SECONDS:-900}
mkdir -p var/logs var/run

if ! [[ "$SITE_BUILD_INTERVAL" == <-> && "$SITE_BUILD_INTERVAL" -gt 0 ]]; then
  echo "PODSEARCH_SITE_BUILD_INTERVAL_SECONDS must be a positive integer" >&2
  exit 1
fi

LOCK_DIR="var/run/backfill.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -f "$LOCK_DIR/pid" ]; then
    LOCK_PID=$(<"$LOCK_DIR/pid")
    if [[ "$LOCK_PID" == <-> ]] && kill -0 "$LOCK_PID" 2>/dev/null; then
      echo "podsearch pipeline already in progress; skipping backfill"
      exit 0
    fi
  fi
  rm -f "$LOCK_DIR/pid" "$LOCK_DIR/started_at"
  rmdir "$LOCK_DIR" 2>/dev/null || {
    echo "podsearch pipeline lock exists and could not be cleared: $LOCK_DIR" >&2
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

python3 -m podsearch --config config.toml sync-catalog
python3 -m podsearch --config config.toml ingest --since 2026-01-01

while true; do
  OUTPUT=$(python3 -m podsearch --config config.toml transcribe \
    --since 2026-01-01 \
    --limit "$BATCH_SIZE" \
    --ranked-round-robin \
    --retry-failed \
    --quiet-command)
  print -r -- "$OUTPUT"
  QUEUED=$(print -r -- "$OUTPUT" | awk -F= '$1 == "queued" {print $2}')
  if [ "${QUEUED:-0}" -eq 0 ]; then
    break
  fi
  python3 -m podsearch --config config.toml build-site \
    --if-stale-seconds "$SITE_BUILD_INTERVAL"
done

python3 -m podsearch --config config.toml build-site
