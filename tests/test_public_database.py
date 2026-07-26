from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import unittest

from podsearch import storage
from podsearch.config import (
    AppConfig,
    ChartConfig,
    Config,
    SiteConfig,
    TranscriptionConfig,
)
from podsearch.site import (
    SCHEMA_VERSION,
    _build_public_databases,
    build_site,
    fold_search_text,
)


NOW = "2026-07-25T00:00:00+00:00"


def _connect_source() -> sqlite3.Connection:
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    storage.migrate(source)
    source.execute(
        """
        INSERT INTO shows (
          apple_id, name, artist, apple_url, feed_url, artwork_url, description,
          chart_rank, in_top_100, ever_top_100, favorite, active, first_seen_at,
          last_seen_at, created_at, updated_at
        )
        VALUES ('1', 'Café Ünicode', 'Publisher', 'https://apple.example',
                'https://feed.example', NULL, 'A show about ÉLÉPHANTS.',
                7, 1, 1, 1, 1, ?, ?, ?, ?)
        """,
        (NOW, NOW, NOW, NOW),
    )
    show_id = source.execute("SELECT id FROM shows").fetchone()["id"]
    source.execute(
        """
        INSERT INTO episodes (
          show_id, guid, title, description, episode_url, image_url, published_at,
          status, transcript_text, created_at, updated_at
        )
        VALUES (?, 'done', 'The Finished Episode', 'Finished metadata summary.',
                'https://episode.example', 'https://image.example',
                '2026-07-25T00:00:00+00:00', 'transcribed',
                'A very specific transcript phrase.', ?, ?),
               (?, 'new', 'Not Ready', 'Pending metadata summary.', NULL, NULL,
                '2026-07-24T00:00:00+00:00', 'new', NULL, ?, ?)
        """,
        (show_id, NOW, NOW, show_id, NOW, NOW),
    )
    # Mirror production writes, where the storage migration and
    # set_transcript() persist a content fingerprint.
    storage.migrate(source)
    source.commit()
    return source


def _build(source: sqlite3.Connection, root: pathlib.Path) -> dict:
    data_dir = root / "data"
    state_dir = root / "state"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return _build_public_databases(
        source, data_dir, state_dir=state_dir, asset_version="0123456789ab"
    )


def _query(path: pathlib.Path, sql: str, params: tuple = ()):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


class PublicDatabaseTests(unittest.TestCase):
    def test_worker_builds_are_deferred_and_a_forced_build_clears_pending(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = pathlib.Path(__file__).resolve().parents[1]
            config = Config(
                root=repository,
                app=AppConfig(
                    database_path=root / "source.sqlite3",
                    public_dir=root / "public",
                    state_dir=root / "var",
                ),
                chart=ChartConfig(),
                transcription=TranscriptionConfig(),
                site=SiteConfig(),
                favorites=(),
                feed_overrides={},
            )
            build_site(config, source)
            manifest = config.app.public_dir / "data" / "manifest.json"
            stamp = manifest.stat().st_mtime_ns

            deferred = build_site(
                config,
                source,
                minimum_interval_seconds=900,
            )
            self.assertEqual(deferred["site_build_deferred"], 1)
            self.assertEqual(manifest.stat().st_mtime_ns, stamp)
            self.assertTrue((config.app.state_dir / "run" / "site-build.pending").is_file())

            build_site(config, source)
            self.assertFalse(
                (config.app.state_dir / "run" / "site-build.pending").exists()
            )
        source.close()

    def test_catalog_excludes_bulk_columns_and_indexes_snippets(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            stats = _build(source, root)
            catalog = root / "data" / "catalog.sqlite3"

            self.assertEqual(stats["public_transcripts"], 1)
            self.assertEqual(stats["transcript_shards"], 1)

            columns = {row[1] for row in _query(catalog, "PRAGMA table_info(episodes)")}
            # These are the columns whose bulk forced the whole-file download.
            for absent in ("transcript_text", "description", "episode_url", "image_url"):
                self.assertNotIn(absent, columns)
            for present in ("snippet", "has_transcript", "has_detail", "transcript_shard"):
                self.assertIn(present, columns)

            self.assertEqual(
                _query(catalog, "SELECT COUNT(*) FROM episodes")[0][0], 2
            )
            self.assertEqual(
                _query(
                    catalog,
                    "SELECT has_transcript, transcript_shard FROM episodes"
                    " WHERE title = 'The Finished Episode'",
                )[0],
                (1, "2026-07"),
            )
            row = _query(
                catalog,
                "SELECT snippet(episode_search, 2, '[', ']', '...', 8)"
                " FROM episode_search WHERE episode_search MATCH 'metadata'",
            )
            self.assertTrue(row)
            self.assertIn("[metadata]", row[0][0])
        source.close()

    def test_details_hold_descriptions_and_links(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            details = root / "data" / "details.sqlite3"
            stored = _query(
                details,
                "SELECT description, episode_url, image_url FROM details"
                " ORDER BY episode_id",
            )
            self.assertTrue(stored)
            self.assertIn(
                ("Finished metadata summary.", "https://episode.example", "https://image.example"),
                stored,
            )

            catalog = root / "data" / "catalog.sqlite3"
            claimed = _query(catalog, "SELECT COUNT(*) FROM episodes WHERE has_detail = 1")[0][0]
            self.assertEqual(claimed, len(stored))
        source.close()

    def test_global_search_index_is_contentless_and_complete(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            search = root / "data" / "search.sqlite3"
            hits = _query(
                search,
                "SELECT rowid FROM transcript_search WHERE transcript_search MATCH 'specific'",
            )
            self.assertEqual(len(hits), 1)
            # A contentless index stores no text, which is what keeps one global
            # index affordable for the whole archive.
            self.assertEqual(
                _query(search, "SELECT COUNT(*) FROM transcript_search")[0][0], 1
            )
        source.close()

    def test_shard_still_produces_snippets(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            shard = root / "data" / "transcripts" / "2026-07.sqlite3"
            row = _query(
                shard,
                "SELECT snippet(transcript_search, 2, '[', ']', '...', 8)"
                " FROM transcript_search WHERE transcript_search MATCH 'specific'",
            )
            self.assertTrue(row)
            self.assertIn("[specific]", row[0][0])
        source.close()

    def test_manifest_describes_every_published_file(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            data_dir = root / "data"
            manifest = json.loads((data_dir / "manifest.json").read_text())

            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
            self.assertEqual(manifest["assets"], "0123456789ab")
            for key in ("catalog", "details", "search"):
                entry = manifest[key]
                relative = entry["path"].split("?")[0].removeprefix("/data/")
                target = data_dir / relative
                self.assertTrue(target.is_file(), relative)
                self.assertEqual(target.stat().st_size, entry["bytes"])
                self.assertEqual(
                    _query(target, "SELECT value FROM meta WHERE key = 'version'")[0][0],
                    entry["version"],
                )
            self.assertEqual(len(manifest["shards"]), 1)
            self.assertEqual(manifest["shards"][0]["key"], "2026-07")
        source.close()

    def test_published_files_carry_no_build_bookkeeping(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            data_dir = root / "data"
            for path in [
                data_dir / "catalog.sqlite3",
                data_dir / "details.sqlite3",
                data_dir / "search.sqlite3",
                data_dir / "transcripts" / "2026-07.sqlite3",
            ]:
                names = {
                    row[0]
                    for row in _query(
                        path, "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertNotIn("build_state", names, path.name)
            # The state that drives incremental builds lives outside public/.
            self.assertTrue(list((root / "state").glob("*.state")))
        source.close()

    def test_unchanged_source_rebuilds_nothing(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = _build(source, root)
            self.assertGreater(first["rebuilt_databases"], 0)
            stamps = {
                path.name: path.stat().st_mtime_ns
                for path in (root / "data").rglob("*.sqlite3")
            }
            second = _build(source, root)
            self.assertEqual(second["rebuilt_databases"], 0)
            for path in (root / "data").rglob("*.sqlite3"):
                self.assertEqual(stamps[path.name], path.stat().st_mtime_ns, path.name)
        source.close()

    def test_new_transcript_leaves_details_untouched(self) -> None:
        """Writing a transcript bumps episodes.updated_at.

        If the details fingerprint depended on that, every backfill pass would
        republish the entire details database for no reason.
        """
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            details = root / "data" / "details.sqlite3"
            details_stamp = details.stat().st_mtime_ns

            episode_id = source.execute(
                "SELECT id FROM episodes WHERE guid = 'new'"
            ).fetchone()["id"]
            storage.set_transcript(
                source,
                int(episode_id),
                transcript="Another transcript entirely.",
                path=root / "new.txt",
            )
            source.commit()
            stats = _build(source, root)

            self.assertEqual(stats["public_transcripts"], 2)
            self.assertEqual(
                details.stat().st_mtime_ns,
                details_stamp,
                "a transcript-only change must not republish details",
            )
            search = root / "data" / "search.sqlite3"
            self.assertEqual(
                _query(search, "SELECT COUNT(*) FROM transcript_search")[0][0], 2
            )
        source.close()

    def test_retranscription_replaces_index_entries(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            episode_id = source.execute(
                "SELECT id FROM episodes WHERE guid = 'done'"
            ).fetchone()["id"]
            storage.set_transcript(
                source,
                int(episode_id),
                transcript="Replacement wording here.",
                path=root / "replacement.txt",
            )
            source.commit()
            _build(source, root)

            search = root / "data" / "search.sqlite3"
            shard = root / "data" / "transcripts" / "2026-07.sqlite3"
            for path in (search, shard):
                self.assertEqual(
                    _query(
                        path,
                        "SELECT COUNT(*) FROM transcript_search"
                        " WHERE transcript_search MATCH 'specific'",
                    )[0][0],
                    0,
                    f"{path.name} kept a stale term",
                )
                self.assertEqual(
                    _query(
                        path,
                        "SELECT COUNT(*) FROM transcript_search"
                        " WHERE transcript_search MATCH 'replacement'",
                    )[0][0],
                    1,
                    f"{path.name} missed the new term",
                )
            self.assertEqual(
                _query(shard, "SELECT COUNT(*) FROM transcripts")[0][0], 1
            )
        source.close()

    def test_routine_feed_refresh_does_not_rebuild_transcript_indexes(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            search = root / "data" / "search.sqlite3"
            shard = root / "data" / "transcripts" / "2026-07.sqlite3"
            stamps = (search.stat().st_mtime_ns, shard.stat().st_mtime_ns)

            source.execute(
                """
                UPDATE episodes
                SET updated_at = '2026-07-29T00:00:00+00:00'
                WHERE guid = 'done'
                """
            )
            source.commit()
            stats = _build(source, root)

            self.assertEqual(stats["rebuilt_databases"], 0)
            self.assertEqual(
                stamps,
                (search.stat().st_mtime_ns, shard.stat().st_mtime_ns),
            )
        source.close()

    def test_show_name_change_refreshes_transcript_indexes(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            source.execute("UPDATE shows SET name = 'Renamed Program'")
            source.commit()
            _build(source, root)

            for path in (
                root / "data" / "search.sqlite3",
                root / "data" / "transcripts" / "2026-07.sqlite3",
            ):
                self.assertEqual(
                    _query(
                        path,
                        "SELECT COUNT(*) FROM transcript_search"
                        " WHERE transcript_search MATCH 'renamed'",
                    )[0][0],
                    1,
                    path.name,
                )
        source.close()

    def test_removed_transcript_disappears_from_every_index(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            source.execute(
                "UPDATE episodes SET status = 'new', transcript_text = NULL,"
                " updated_at = '2026-07-28T00:00:00+00:00' WHERE guid = 'done'"
            )
            source.commit()
            stats = _build(source, root)

            self.assertEqual(stats["public_transcripts"], 0)
            search = root / "data" / "search.sqlite3"
            self.assertEqual(
                _query(search, "SELECT COUNT(*) FROM transcript_search")[0][0], 0
            )
            # The month has no transcripts left, so its shard is pruned.
            self.assertFalse((root / "data" / "transcripts" / "2026-07.sqlite3").exists())
            catalog = root / "data" / "catalog.sqlite3"
            self.assertEqual(
                _query(catalog, "SELECT COUNT(*) FROM episodes WHERE has_transcript = 1")[0][0],
                0,
            )
        source.close()

    def test_show_search_text_is_unicode_folded(self) -> None:
        source = _connect_source()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _build(source, root)
            catalog = root / "data" / "catalog.sqlite3"
            search_text = _query(catalog, "SELECT search_text FROM shows")[0][0]

            # SQLite's lower() only folds ASCII, so the accented name had to be
            # folded at build time for a lowercase query to match it.
            self.assertIn("cafe unicode", search_text)
            self.assertIn("elephants", search_text)
            matched = _query(
                catalog,
                "SELECT COUNT(*) FROM shows WHERE search_text LIKE ?",
                (f"%{fold_search_text('ÉLÉPHANTS')}%",),
            )[0][0]
            self.assertEqual(matched, 1)
        source.close()

    def test_fold_search_text_matches_browser_lowercasing(self) -> None:
        self.assertEqual(fold_search_text("ÉLÉPHANT"), "elephant")
        self.assertEqual(fold_search_text("Café"), "cafe")
        self.assertEqual(fold_search_text(None), "")
        self.assertEqual(fold_search_text("Ünicode  Show"), "unicode  show")


if __name__ == "__main__":
    unittest.main()
