#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
export PYTHONDONTWRITEBYTECODE=1

MINI_HOST=${PODSEARCH_MINI_HOST:?Set PODSEARCH_MINI_HOST to the Mac mini Tailscale SSH host}
MINI_REPO=${PODSEARCH_MINI_REPO:-/Users/merimerimeri/Development/podsearch}
WORKER_ID=${PODSEARCH_WORKER_ID:-$(scutil --get LocalHostName 2>/dev/null || hostname -s)}
WORKER_ID=$(print -r -- "$WORKER_ID" | tr -cd 'A-Za-z0-9._-')
CLAIM_LIMIT=${PODSEARCH_WORKER_CLAIM_LIMIT:-200}
LEASE_HOURS=${PODSEARCH_WORKER_LEASE_HOURS:-72}
WHISPER_MODEL=${PODSEARCH_WHISPER_MODEL:-$HOME/.cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin}
LOCAL_DATABASE="$PWD/var/remote-worker-$WORKER_ID.sqlite3"
INCOMING_DATABASE="$PWD/var/.remote-worker-$WORKER_ID.sqlite3.incoming"
OUTBOX="$PWD/var/worker-outbox"
REMOTE_SNAPSHOT="$MINI_REPO/var/worker-exports/$WORKER_ID.sqlite3"

if [ -z "$WORKER_ID" ]; then
  echo "Could not determine a safe worker ID" >&2
  exit 1
fi
for command in python3 ssh scp whisper-cli; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done
if [ ! -f "$WHISPER_MODEL" ]; then
  echo "Whisper model not found: $WHISPER_MODEL" >&2
  echo "Set PODSEARCH_WHISPER_MODEL to ggml-large-v3-turbo-q5_0.bin." >&2
  exit 1
fi
if ! [[ "$CLAIM_LIMIT" == <-> && "$CLAIM_LIMIT" -gt 0 ]]; then
  echo "PODSEARCH_WORKER_CLAIM_LIMIT must be a positive integer" >&2
  exit 1
fi
if ! [[ "$LEASE_HOURS" == <-> && "$LEASE_HOURS" -gt 0 ]]; then
  echo "PODSEARCH_WORKER_LEASE_HOURS must be a positive integer" >&2
  exit 1
fi

mkdir -p var "$OUTBOX"
PENDING_BUNDLES=("$OUTBOX"/*.json(N))
if [ "${#PENDING_BUNDLES[@]}" -gt 0 ]; then
  echo "${#PENDING_BUNDLES[@]} transcript bundle(s) are still waiting for the Mac mini to pull them." >&2
  echo "Wait for the Mini pull job, then rerun this script." >&2
  exit 1
fi

printf -v mini_repo_q %q "$MINI_REPO"
printf -v remote_snapshot_q %q "$REMOTE_SNAPSHOT"
printf -v worker_id_q %q "$WORKER_ID"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$MINI_HOST" \
  "cd $mini_repo_q && mkdir -p var/worker-exports && python3 -m podsearch --config config.toml export-worker-snapshot --output $remote_snapshot_q --worker-id $worker_id_q --claim-limit $CLAIM_LIMIT --lease-hours $LEASE_HOURS --since 2026-01-01"
scp -q -o BatchMode=yes -o ConnectTimeout=10 \
  "$MINI_HOST:$REMOTE_SNAPSHOT" "$INCOMING_DATABASE"
mv -f "$INCOMING_DATABASE" "$LOCAL_DATABASE"

export PODSEARCH_DATABASE_PATH="$LOCAL_DATABASE"
export PODSEARCH_WHISPER_MODEL="$WHISPER_MODEL"

echo "worker_id=$WORKER_ID"
echo "database=$LOCAL_DATABASE"
echo "outbox=$OUTBOX"
echo "selection=oldest leased episodes first"

python3 -m podsearch --config config.toml worker-transcribe \
  --worker-id "$WORKER_ID" \
  --outbox "$OUTBOX" \
  --limit "$CLAIM_LIMIT" \
  --retry-failed

echo "The current lease was attempted once. The Mini will pull completed bundles automatically."
echo "After the outbox is empty, rerun this script for the next oldest block."
