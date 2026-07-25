from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import sqlite3
import sys
from typing import Any

from . import storage, transcriber
from .config import Config
from .text import slugify


BUNDLE_SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 25 * 1024 * 1024


def export_worker_snapshot(
    conn: sqlite3.Connection,
    output: pathlib.Path,
    *,
    worker_id: str,
    claim_limit: int,
    lease_hours: int,
    published_since: str | None,
) -> dict[str, int]:
    claimed_ids = storage.claim_remote_backfill(
        conn,
        worker_id=worker_id,
        limit=claim_limit,
        lease_hours=lease_hours,
        published_since=published_since,
    )
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.tmp")
    temp.unlink(missing_ok=True)
    target = sqlite3.connect(temp)
    try:
        conn.backup(target)
        target.execute("PRAGMA foreign_keys = OFF")
        if claimed_ids:
            placeholders = ",".join("?" for _ in claimed_ids)
            target.execute(
                f"DELETE FROM episodes WHERE id NOT IN ({placeholders})",
                claimed_ids,
            )
            target.execute(
                f"DELETE FROM transcription_claims WHERE episode_id NOT IN ({placeholders})",
                claimed_ids,
            )
        else:
            target.execute("DELETE FROM episodes")
            target.execute("DELETE FROM transcription_claims")
        target.execute(
            "DELETE FROM transcription_claims WHERE worker_id != ?",
            (worker_id,),
        )
        target.execute(
            "DELETE FROM shows WHERE id NOT IN (SELECT DISTINCT show_id FROM episodes)"
        )
        target.execute("DELETE FROM chart_snapshots")
        target.execute("DELETE FROM pipeline_state")
        target.execute(
            """
            UPDATE episodes
            SET transcript_text = NULL,
                transcript_path = NULL,
                error_message = NULL
            """
        )
        target.commit()
        target.execute("VACUUM")
    finally:
        target.close()
    os.replace(temp, output)
    return {
        "claimed": len(claimed_ids),
        "snapshot_bytes": output.stat().st_size,
    }


def process_worker_batch(
    config: Config,
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    outbox: pathlib.Path,
    limit: int,
    retry_failed: bool,
    command_output: bool,
) -> dict[str, int]:
    episodes = storage.episodes_for_remote_worker(
        conn,
        worker_id=worker_id,
        limit=limit,
        retry_failed=retry_failed,
    )
    stats = {"queued": len(episodes), "transcribed": 0, "failed": 0, "exported": 0}
    for episode in episodes:
        episode_id = int(episode["id"])
        try:
            transcriber.transcribe_episode(
                config,
                conn,
                episode,
                command_output=command_output,
            )
        except Exception as exc:  # noqa: BLE001 - keep the worker moving
            storage.mark_transcription_failed(conn, episode_id, str(exc))
            conn.commit()
            stats["failed"] += 1
            continue

        stats["transcribed"] += 1
        try:
            write_result_bundle(
                conn,
                episode_id=episode_id,
                worker_id=worker_id,
                outbox=outbox,
            )
        except Exception as exc:  # noqa: BLE001 - make the local result retryable
            conn.execute(
                """
                UPDATE episodes
                SET status = 'new',
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (f"result bundle export failed: {exc}", storage.now_iso(), episode_id),
            )
            conn.commit()
            stats["failed"] += 1
        else:
            stats["exported"] += 1
    return stats


def write_result_bundle(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    worker_id: str,
    outbox: pathlib.Path,
) -> pathlib.Path:
    row = conn.execute(
        """
        SELECT episodes.id, episodes.guid, episodes.title,
               episodes.published_at, episodes.transcript_text,
               episodes.transcribed_at, shows.apple_id, shows.name AS show_name
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE episodes.id = ?
          AND episodes.status = 'transcribed'
          AND episodes.transcript_text IS NOT NULL
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"episode {episode_id} does not have a completed transcript")
    transcript = str(row["transcript_text"])
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "worker_id": worker_id,
        "worker_host": platform.node(),
        "episode": {
            "id": int(row["id"]),
            "guid": str(row["guid"]),
            "title": str(row["title"]),
            "published_at": row["published_at"],
            "show_apple_id": str(row["apple_id"]),
            "show_name": str(row["show_name"]),
        },
        "transcript": {
            "text": transcript,
            "sha256": digest,
            "completed_at": row["transcribed_at"] or storage.now_iso(),
        },
    }
    outbox = outbox.expanduser().resolve()
    outbox.mkdir(parents=True, exist_ok=True)
    output = outbox / f"{episode_id}-{digest[:12]}.json"
    temp = output.with_name(f".{output.name}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temp, output)
    return output


def import_result_bundles(
    config: Config,
    conn: sqlite3.Connection,
    inbox: pathlib.Path,
) -> dict[str, int]:
    inbox = inbox.expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    stats = {"found": 0, "imported": 0, "skipped": 0, "failed": 0}
    for path in sorted(inbox.glob("*.json")):
        stats["found"] += 1
        try:
            payload = _read_bundle(path)
            result = _import_bundle(config, conn, payload)
        except Exception as exc:  # noqa: BLE001 - retain invalid bundles for inspection
            if conn.in_transaction:
                conn.rollback()
            print(f"failed to import {path.name}: {exc}", file=sys.stderr)
            stats["failed"] += 1
            continue
        path.unlink()
        stats[result] += 1
    return stats


def _read_bundle(path: pathlib.Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError(f"bundle is too large: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported transcript bundle schema")
    episode = payload.get("episode")
    transcript = payload.get("transcript")
    if not isinstance(episode, dict) or not isinstance(transcript, dict):
        raise ValueError("transcript bundle is missing episode data")
    text = transcript.get("text")
    digest = transcript.get("sha256")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("transcript bundle has no transcript text")
    if not isinstance(digest, str) or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise ValueError("transcript bundle checksum does not match")
    for key in ("guid", "show_apple_id"):
        if not isinstance(episode.get(key), str) or not episode[key]:
            raise ValueError(f"transcript bundle is missing {key}")
    return payload


def _import_bundle(
    config: Config,
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> str:
    episode_payload = payload["episode"]
    transcript_payload = payload["transcript"]
    conn.execute("BEGIN IMMEDIATE")
    episode = conn.execute(
        """
        SELECT episodes.id, episodes.title, episodes.status,
               episodes.transcript_text
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE shows.apple_id = ? AND episodes.guid = ?
        """,
        (episode_payload["show_apple_id"], episode_payload["guid"]),
    ).fetchone()
    if episode is None:
        raise ValueError("episode from transcript bundle is not in the primary database")
    episode_id = int(episode["id"])
    if episode["status"] == "transcribed" and episode["transcript_text"]:
        storage.release_transcription_claim(conn, episode_id)
        conn.commit()
        return "skipped"

    transcript = str(transcript_payload["text"])
    transcript_dir = config.transcription.transcript_dir
    transcript_dir.mkdir(parents=True, exist_ok=True)
    output = transcript_dir / f"{episode_id}-{slugify(str(episode['title']))[:80]}.txt"
    temp = output.with_name(f".{output.name}.import")
    temp.write_text(transcript, encoding="utf-8")
    os.replace(temp, output)
    storage.set_transcript(conn, episode_id, transcript=transcript, path=output)
    completed_at = transcript_payload.get("completed_at")
    if isinstance(completed_at, str) and completed_at:
        conn.execute(
            "UPDATE episodes SET transcribed_at = ? WHERE id = ?",
            (completed_at, episode_id),
        )
    storage.release_transcription_claim(conn, episode_id)
    conn.commit()
    return "imported"
