from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from podsearch import transcriber
from podsearch.config import load_config


class _InterruptedResponse:
    headers: dict[str, str] = {}

    def __enter__(self) -> "_InterruptedResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        if not hasattr(self, "_read_once"):
            self._read_once = True
            return b"partial audio"
        raise OSError("connection interrupted")


class AudioDownloadTests(unittest.TestCase):
    def test_interrupted_download_never_looks_like_complete_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            config = load_config(config_path)
            output = root / "episode.mp3"

            with mock.patch(
                "urllib.request.urlopen",
                return_value=_InterruptedResponse(),
            ):
                with self.assertRaises(OSError):
                    transcriber._download_audio(
                        config,
                        "https://example.com/episode.mp3",
                        output,
                    )

            self.assertFalse(output.exists())
            self.assertFalse((root / ".episode.mp3.part").exists())


if __name__ == "__main__":
    unittest.main()
