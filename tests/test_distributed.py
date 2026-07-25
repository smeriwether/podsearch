from __future__ import annotations

import contextlib
import io
import pathlib
import sqlite3
import tempfile
import unittest

from podsearch import distributed, storage
from podsearch.config import load_config


class DistributedBackfillTests(unittest.TestCase):
    def test_snapshot_bundle_and_idempotent_import(self) -> None:
        source = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        storage.migrate(source)
        now = storage.now_iso()
        source.execute(
            """
            INSERT INTO shows (
              apple_id, name, chart_rank, in_top_100, ever_top_100, active,
              first_seen_at, last_seen_at, created_at, updated_at
            )
            VALUES ('show-1', 'Example Show', 1, 1, 1, 1, ?, ?, ?, ?)
            """,
            (now, now, now, now),
        )
        show_id = source.execute("SELECT id FROM shows").fetchone()["id"]
        source.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, audio_url, published_at, status,
              created_at, updated_at
            )
            VALUES (?, 'episode-guid', 'Oldest Episode',
                    'https://example.com/audio.mp3',
                    '2026-01-02T00:00:00+00:00', 'new', ?, ?)
            """,
            (show_id, now, now),
        )
        source.commit()

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[app]
database_path = "primary.sqlite3"
[transcription]
audio_dir = "audio"
transcript_dir = "transcripts"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            snapshot = root / "worker.sqlite3"
            stats = distributed.export_worker_snapshot(
                source,
                snapshot,
                worker_id="macbook-pro",
                claim_limit=10,
                lease_hours=72,
                published_since="2026-01-01",
            )
            self.assertEqual(stats["claimed"], 1)

            worker = sqlite3.connect(snapshot)
            worker.row_factory = sqlite3.Row
            self.assertEqual(worker.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
            self.assertIsNone(
                worker.execute("SELECT transcript_text FROM episodes").fetchone()[0]
            )
            episode_id = worker.execute("SELECT id FROM episodes").fetchone()["id"]
            worker_transcript_path = root / "worker-transcript.txt"
            storage.set_transcript(
                worker,
                episode_id,
                transcript="A completed remote transcript.",
                path=worker_transcript_path,
            )
            worker.commit()
            outbox = root / "outbox"
            distributed.write_result_bundle(
                worker,
                episode_id=episode_id,
                worker_id="macbook-pro",
                outbox=outbox,
            )

            imported = distributed.import_result_bundles(config, source, outbox)
            self.assertEqual(imported["imported"], 1)
            row = source.execute(
                """
                SELECT status, transcript_text, transcript_path
                FROM episodes
                WHERE guid = 'episode-guid'
                """
            ).fetchone()
            self.assertEqual(row["status"], "transcribed")
            self.assertEqual(row["transcript_text"], "A completed remote transcript.")
            self.assertTrue(pathlib.Path(row["transcript_path"]).is_file())
            self.assertEqual(
                source.execute("SELECT COUNT(*) FROM transcription_claims").fetchone()[0],
                0,
            )
            self.assertEqual(list(outbox.glob("*.json")), [])

            distributed.write_result_bundle(
                worker,
                episode_id=episode_id,
                worker_id="macbook-pro",
                outbox=outbox,
            )
            duplicate = distributed.import_result_bundles(config, source, outbox)
            self.assertEqual(duplicate["skipped"], 1)
            self.assertEqual(duplicate["imported"], 0)

            invalid_bundle = outbox / "invalid.json"
            invalid_bundle.write_text(
                """
                {
                  "schema_version": 1,
                  "episode": {
                    "guid": "episode-guid",
                    "show_apple_id": "show-1"
                  },
                  "transcript": {
                    "text": "Tampered transcript.",
                    "sha256": "not-the-real-checksum"
                  }
                }
                """,
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                invalid = distributed.import_result_bundles(config, source, outbox)
            self.assertEqual(invalid["failed"], 1)
            self.assertTrue(invalid_bundle.is_file())
            worker.close()
        source.close()


if __name__ == "__main__":
    unittest.main()
