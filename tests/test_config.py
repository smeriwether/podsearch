from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from podsearch.config import load_config


class ConfigTests(unittest.TestCase):
    def test_paths_and_favorites_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            config_path = root / "config.toml"
            config_path.write_text(
                """
favorites = ["1322200189"]
[app]
database_path = "var/test.sqlite3"
[chart]
country = "us"
limit = 100
[transcription]
audio_dir = "var/audio"
transcript_dir = "var/transcripts"
[site]
port = 9999
[feed_overrides]
"1322200189" = "https://example.com/feed.xml"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.favorites, ("1322200189",))
            self.assertEqual(config.app.database_path, root / "var/test.sqlite3")
            self.assertEqual(config.site.port, 9999)
            self.assertEqual(
                config.feed_overrides["1322200189"],
                "https://example.com/feed.xml",
            )
            self.assertIn("/top/100/", config.chart.resolved_url)

    def test_worker_environment_overrides_database_and_whisper_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            config_path = root / "config.toml"
            config_path.write_text(
                """
[app]
database_path = "var/primary.sqlite3"
[transcription]
args = ["-m", "models/default.bin", "-f", "{audio_path}"]
""",
                encoding="utf-8",
            )
            worker_database = root / "worker.sqlite3"
            worker_model = root / "models/large-turbo.bin"
            with mock.patch.dict(
                "os.environ",
                {
                    "PODSEARCH_DATABASE_PATH": str(worker_database),
                    "PODSEARCH_WHISPER_MODEL": str(worker_model),
                },
                clear=False,
            ):
                config = load_config(config_path)

            self.assertEqual(config.app.database_path, worker_database)
            self.assertEqual(
                config.transcription.args,
                ("-m", str(worker_model), "-f", "{audio_path}"),
            )
