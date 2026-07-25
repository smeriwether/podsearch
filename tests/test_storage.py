from __future__ import annotations

import sqlite3
import unittest

from podsearch import storage


class RankedBackfillQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage.migrate(self.conn)
        now = storage.now_iso()
        for rank in (1, 2):
            self.conn.execute(
                """
                INSERT INTO shows (
                  apple_id, name, chart_rank, in_top_100, active,
                  first_seen_at, last_seen_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (str(rank), f"Show {rank}", rank, now, now, now, now),
            )
        self.show_ids = {
            int(row["chart_rank"]): int(row["id"])
            for row in self.conn.execute("SELECT id, chart_rank FROM shows")
        }
        for rank in (1, 2):
            for day in (1, 2, 3):
                self._add_episode(rank, f"r{rank}-{day}", day)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _add_episode(self, rank: int, guid: str, day: int) -> None:
        timestamp = storage.now_iso()
        self.conn.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, audio_url, published_at,
              status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                self.show_ids[rank],
                guid,
                guid,
                f"https://example.com/{guid}.mp3",
                f"2026-07-{day:02d}T12:00:00+00:00",
                timestamp,
                timestamp,
            ),
        )

    def _next(self) -> list[sqlite3.Row]:
        return storage.episodes_for_transcription(
            self.conn,
            limit=2,
            published_since="2026-01-01",
            retry_failed=False,
            ranked_round_robin=True,
        )

    def test_cycles_by_rank_and_prioritizes_new_episodes_next_round(self) -> None:
        rank_one = self._next()
        self.assertEqual([row["guid"] for row in rank_one], ["r1-3", "r1-2"])
        self.assertEqual({row["queue_chart_rank"] for row in rank_one}, {1})
        self.conn.executemany(
            "UPDATE episodes SET status = 'transcribed' WHERE id = ?",
            [(row["id"],) for row in rank_one],
        )
        storage.advance_ranked_backfill_cursor(
            self.conn, completed_chart_rank=1
        )

        rank_two = self._next()
        self.assertEqual([row["guid"] for row in rank_two], ["r2-3", "r2-2"])
        self.conn.executemany(
            "UPDATE episodes SET status = 'transcribed' WHERE id = ?",
            [(row["id"],) for row in rank_two],
        )
        storage.advance_ranked_backfill_cursor(
            self.conn, completed_chart_rank=2
        )

        self._add_episode(1, "r1-new", 25)
        wrapped_rank_one = self._next()
        self.assertEqual(
            [row["guid"] for row in wrapped_rank_one],
            ["r1-new", "r1-1"],
        )
        self.assertEqual(wrapped_rank_one[0]["queue_cursor_rank"], 3)


class CatalogOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _upsert(self, apple_id: str, name: str, rank: int | None, favorite: bool) -> None:
        storage.upsert_show(
            self.conn,
            apple_id=apple_id,
            name=name,
            artist=None,
            apple_url=None,
            feed_url=None,
            artwork_url=None,
            genres=(),
            chart_rank=rank,
            favorite=favorite,
        )

    def test_ranked_shows_precede_alphabetical_out_of_chart_favorites(self) -> None:
        self._upsert("rank-2", "Rank Two", 2, True)
        self._upsert("favorite-z", "Zulu Favorite", None, True)
        self._upsert("rank-1", "Rank One", 1, False)
        self._upsert("favorite-a", "Alpha Favorite", None, True)

        self.assertEqual(
            [row["apple_id"] for row in storage.active_shows(self.conn)],
            ["rank-1", "rank-2", "favorite-a", "favorite-z"],
        )

    def test_catalog_refresh_replaces_the_favorite_flag(self) -> None:
        self._upsert("show", "Show", 1, True)
        storage.mark_favorites_stale(self.conn)
        self._upsert("show", "Show", 1, False)

        row = self.conn.execute(
            "SELECT favorite FROM shows WHERE apple_id = 'show'"
        ).fetchone()
        self.assertEqual(row["favorite"], 0)

    def test_former_chart_show_stays_in_feed_refresh_but_leaves_transcription_queue(self) -> None:
        self._upsert("former", "Former Show", 42, False)
        show_id = self.conn.execute(
            "SELECT id FROM shows WHERE apple_id = 'former'"
        ).fetchone()["id"]
        timestamp = storage.now_iso()
        self.conn.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, audio_url, published_at,
              status, created_at, updated_at
            )
            VALUES (?, 'former-episode', 'Former Episode',
                    'https://example.com/former.mp3',
                    '2026-07-25T12:00:00+00:00', 'new', ?, ?)
            """,
            (show_id, timestamp, timestamp),
        )
        self.conn.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, published_at, status, transcript_text,
              created_at, updated_at
            )
            VALUES (?, 'completed-episode', 'Completed Episode',
                    '2026-07-24T12:00:00+00:00', 'transcribed',
                    'Keep this completed transcript.', ?, ?)
            """,
            (show_id, timestamp, timestamp),
        )

        storage.mark_chart_stale(self.conn)

        show = storage.active_shows(self.conn)[0]
        self.assertEqual(show["apple_id"], "former")
        self.assertEqual(show["in_top_100"], 0)
        self.assertEqual(show["ever_top_100"], 1)
        self.assertEqual(show["chart_rank"], 42)
        self.assertEqual(
            storage.episodes_for_transcription(
                self.conn,
                limit=10,
                published_since="2026-01-01",
                retry_failed=True,
            ),
            [],
        )
        preserved = self.conn.execute(
            """
            SELECT transcript_text
            FROM episodes
            WHERE guid = 'completed-episode'
            """
        ).fetchone()
        self.assertEqual(preserved["transcript_text"], "Keep this completed transcript.")

    def test_migration_recovers_a_former_shows_last_known_rank(self) -> None:
        self._upsert("former", "Former Show", 42, False)
        storage.add_chart_snapshot(
            self.conn,
            captured_at="2026-07-25T12:00:00+00:00",
            country="us",
            rank=42,
            apple_id="former",
        )
        self.conn.execute(
            "UPDATE shows SET in_top_100 = 0, chart_rank = NULL WHERE apple_id = 'former'"
        )

        storage.migrate(self.conn)

        row = self.conn.execute(
            "SELECT chart_rank FROM shows WHERE apple_id = 'former'"
        ).fetchone()
        self.assertEqual(row["chart_rank"], 42)

    def test_out_of_chart_favorite_remains_in_transcription_queue(self) -> None:
        self._upsert("favorite", "Favorite Show", None, True)
        show_id = self.conn.execute(
            "SELECT id FROM shows WHERE apple_id = 'favorite'"
        ).fetchone()["id"]
        timestamp = storage.now_iso()
        self.conn.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, audio_url, published_at,
              status, created_at, updated_at
            )
            VALUES (?, 'favorite-episode', 'Favorite Episode',
                    'https://example.com/favorite.mp3',
                    '2026-07-25T12:00:00+00:00', 'new', ?, ?)
            """,
            (show_id, timestamp, timestamp),
        )

        queued = storage.episodes_for_transcription(
            self.conn,
            limit=10,
            published_since="2026-01-01",
            retry_failed=True,
        )
        self.assertEqual([row["guid"] for row in queued], ["favorite-episode"])


if __name__ == "__main__":
    unittest.main()
