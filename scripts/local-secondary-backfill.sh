#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
export PYTHONDONTWRITEBYTECODE=1

WORKER_ID=${PODSEARCH_SECONDARY_WORKER_ID:-mac-mini-secondary}
LEASE_HOURS=${PODSEARCH_SECONDARY_LEASE_HOURS:-24}
RETRY_DELAY=${PODSEARCH_SECONDARY_RETRY_DELAY:-300}
MIN_MEMORY_FREE_PERCENT=${PODSEARCH_SECONDARY_MIN_MEMORY_FREE_PERCENT:-25}
SITE_BUILD_INTERVAL=${PODSEARCH_SITE_BUILD_INTERVAL_SECONDS:-900}
DATABASE="$PWD/var/local-secondary.sqlite3"
OUTBOX="$PWD/var/local-secondary-outbox"

if ! [[ "$LEASE_HOURS" == <-> && "$LEASE_HOURS" -gt 0 ]]; then
  echo "PODSEARCH_SECONDARY_LEASE_HOURS must be a positive integer" >&2
  exit 1
fi
if ! [[ "$RETRY_DELAY" == <-> && "$RETRY_DELAY" -gt 0 ]]; then
  echo "PODSEARCH_SECONDARY_RETRY_DELAY must be a positive integer" >&2
  exit 1
fi
if ! [[ "$MIN_MEMORY_FREE_PERCENT" == <-> && "$MIN_MEMORY_FREE_PERCENT" -gt 0 && "$MIN_MEMORY_FREE_PERCENT" -lt 100 ]]; then
  echo "PODSEARCH_SECONDARY_MIN_MEMORY_FREE_PERCENT must be between 1 and 99" >&2
  exit 1
fi
if ! [[ "$SITE_BUILD_INTERVAL" == <-> && "$SITE_BUILD_INTERVAL" -gt 0 ]]; then
  echo "PODSEARCH_SITE_BUILD_INTERVAL_SECONDS must be a positive integer" >&2
  exit 1
fi

mkdir -p var/logs var/run "$OUTBOX"
LOCK_DIR="var/run/local-secondary-backfill.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  EXISTING_PID=$(<"$LOCK_DIR/pid" 2>/dev/null || true)
  if [[ "$EXISTING_PID" == <-> ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "Secondary backfill is already running as PID $EXISTING_PID; skipping."
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Could not recover the stale secondary backfill lock." >&2
    exit 1
  fi
fi
print -r -- "$$" > "$LOCK_DIR/pid"
cleanup_lock() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

while true; do
  MEMORY_FREE=$(memory_pressure -Q | awk '/free percentage:/ {gsub("%", "", $NF); print $NF}')
  if [[ "$MEMORY_FREE" == <-> && "$MEMORY_FREE" -lt "$MIN_MEMORY_FREE_PERCENT" ]]; then
    echo "Memory headroom is ${MEMORY_FREE}%; pausing the secondary worker for ${RETRY_DELAY}s."
    sleep "$RETRY_DELAY"
    continue
  fi

  EXPORT_OUTPUT=$(python3 -m podsearch --config config.toml \
    export-worker-snapshot \
    --output "$DATABASE" \
    --worker-id "$WORKER_ID" \
    --claim-limit 1 \
    --lease-hours "$LEASE_HOURS" \
    --since 2026-01-01)
  print -r -- "$EXPORT_OUTPUT"
  CLAIMED=$(print -r -- "$EXPORT_OUTPUT" | awk -F= '$1 == "claimed" {print $2}')
  if [ "${CLAIMED:-0}" -eq 0 ]; then
    echo "No eligible secondary backfill work remains."
    break
  fi

  TRANSCRIBE_OUTPUT=$(env PODSEARCH_DATABASE_PATH="$DATABASE" \
    python3 -m podsearch --config config.toml worker-transcribe \
      --worker-id "$WORKER_ID" \
      --outbox "$OUTBOX" \
      --limit 1 \
      --retry-failed \
      --quiet-command)
  print -r -- "$TRANSCRIBE_OUTPUT"
  EXPORTED=$(print -r -- "$TRANSCRIBE_OUTPUT" | awk -F= '$1 == "exported" {print $2}')
  if [ "${EXPORTED:-0}" -eq 0 ]; then
    echo "Secondary transcription failed; retrying in ${RETRY_DELAY}s." >&2
    sleep "$RETRY_DELAY"
    continue
  fi

  python3 -m podsearch --config config.toml \
    import-transcript-results "$OUTBOX"
  python3 -m podsearch --config config.toml build-site \
    --if-stale-seconds "$SITE_BUILD_INTERVAL"
done

python3 -m podsearch --config config.toml build-site
