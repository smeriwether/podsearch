from __future__ import annotations

import pathlib
import plistlib
import tempfile
import unittest
from unittest import mock

from podsearch import launchd
from podsearch.config import load_config


class RemotePullLaunchAgentTests(unittest.TestCase):
    def test_install_remote_pull_writes_periodic_worker_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            home = root / "home"
            config_path = root / "config.toml"
            config_path.write_text(
                """
[app]
state_dir = "var"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            with mock.patch("pathlib.Path.home", return_value=home):
                path = launchd.install_remote_pull(
                    config,
                    worker="macbook.example.ts.net",
                    remote_repo="/Users/example/Development/podsearch",
                    interval_seconds=300,
                )

            with path.open("rb") as source:
                payload = plistlib.load(source)
            self.assertEqual(payload["Label"], launchd.REMOTE_PULL_LABEL)
            self.assertEqual(payload["StartInterval"], 300)
            self.assertTrue(payload["RunAtLoad"])
            self.assertEqual(
                payload["EnvironmentVariables"]["PODSEARCH_REMOTE_WORKER"],
                "macbook.example.ts.net",
            )
            self.assertEqual(
                payload["EnvironmentVariables"]["PODSEARCH_REMOTE_REPO"],
                "/Users/example/Development/podsearch",
            )

    def test_install_remote_pull_validates_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            config = load_config(config_path)
            with self.assertRaises(ValueError):
                launchd.install_remote_pull(
                    config,
                    worker="worker",
                    remote_repo="relative/path",
                    interval_seconds=300,
                )
            with self.assertRaises(ValueError):
                launchd.install_remote_pull(
                    config,
                    worker="worker",
                    remote_repo="/absolute/path",
                    interval_seconds=30,
                )


if __name__ == "__main__":
    unittest.main()
