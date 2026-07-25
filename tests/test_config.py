from __future__ import annotations

import pathlib
import tempfile
import unittest

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
