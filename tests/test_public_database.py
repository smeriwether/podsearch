from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest

from podsearch import storage
from podsearch.site import _build_public_databases


class PublicDatabaseTests(unittest.TestCase):
    def test_export_splits_catalog_metadata_from_monthly_transcript_search(self) -> None:
        source = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        storage.migrate(source)
        now = storage.now_iso()
        source.execute(
            """
            INSERT INTO shows (
              apple_id, name, artist, apple_url, feed_url, artwork_url,
              chart_rank, in_top_100, ever_top_100, favorite, active, first_seen_at,
              last_seen_at, created_at, updated_at
            )
            VALUES ('1', 'Example Show', 'Publisher', 'https://apple.example',
                    'https://feed.example', NULL, 7, 1, 1, 1, 1, ?, ?, ?, ?)
            """,
            (now, now, now, now),
        )
        show_id = source.execute("SELECT id FROM shows").fetchone()["id"]
        source.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, description, episode_url, published_at, status,
              transcript_text, created_at, updated_at
            )
            VALUES (?, 'done', 'The Finished Episode', 'Finished metadata summary.',
                    'https://episode.example',
                    '2026-07-25T00:00:00+00:00', 'transcribed',
                    'A very specific transcript phrase.', ?, ?),
                   (?, 'new', 'Not Ready', 'Pending metadata summary.', NULL,
                    '2026-07-24T00:00:00+00:00', 'new', NULL, ?, ?)
            """,
            (show_id, now, now, show_id, now, now),
        )
        source.commit()

        with tempfile.TemporaryDirectory() as directory:
            data_dir = pathlib.Path(directory)
            stats = _build_public_databases(source, data_dir)
            catalog_path = data_dir / "catalog.sqlite3"
            shard_path = data_dir / "transcripts" / "2026-07.sqlite3"
            self.assertTrue((data_dir / "catalog.sqlite3.gz").is_file())
            self.assertTrue((data_dir / "transcripts" / "2026-07.sqlite3.gz").is_file())
            self.assertEqual(stats["transcript_shards"], 1)
            self.assertEqual(stats["public_transcripts"], 1)

            public = sqlite3.connect(catalog_path)
            self.assertEqual(
                public.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                2,
            )
            self.assertEqual(
                public.execute(
                    "SELECT value FROM meta WHERE key = 'transcript_count'"
                ).fetchone()[0],
                "1",
            )
            self.assertEqual(
                public.execute(
                    "SELECT in_top_100, ever_top_100 FROM shows"
                ).fetchone(),
                (1, 1),
            )
            columns = {
                row[1] for row in public.execute("PRAGMA table_info(episodes)").fetchall()
            }
            self.assertNotIn("transcript_text", columns)
            self.assertEqual(
                public.execute(
                    """
                    SELECT has_transcript, transcript_shard
                    FROM episodes
                    WHERE title = 'The Finished Episode'
                    """
                ).fetchone(),
                (1, "2026-07"),
            )
            row = public.execute(
                """
                SELECT rowid, snippet(episode_search, 2, '[', ']', '...', 8)
                FROM episode_search
                WHERE episode_search MATCH 'metadata'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("[metadata]", row[1])
            self.assertEqual(
                public.execute(
                    "SELECT key, episode_count FROM transcript_shards"
                ).fetchone(),
                ("2026-07", 1),
            )
            public.close()

            shard = sqlite3.connect(shard_path)
            transcript_row = shard.execute(
                """
                SELECT transcripts.episode_id,
                       snippet(transcript_search, 2, '[', ']', '...', 8)
                FROM transcript_search
                JOIN transcripts ON transcripts.episode_id = transcript_search.rowid
                WHERE transcript_search MATCH 'specific'
                """
            ).fetchone()
            self.assertIsNotNone(transcript_row)
            self.assertIn("[specific]", transcript_row[1])
            self.assertEqual(
                shard.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
                1,
            )
            shard.close()

            shard_mtime = shard_path.stat().st_mtime_ns
            second_stats = _build_public_databases(source, data_dir)
            self.assertEqual(second_stats["transcript_shards"], 1)
            self.assertEqual(
                shard_path.stat().st_mtime_ns,
                shard_mtime,
                "unchanged transcript shards should be reused",
            )
        source.close()


if __name__ == "__main__":
    unittest.main()
