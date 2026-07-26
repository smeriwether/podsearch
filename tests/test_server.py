from __future__ import annotations

import gzip
import http.client
import pathlib
import tempfile
import threading
import unittest
from functools import partial

import http.server

from podsearch.server import StaticHandler


BODY = bytes(range(256)) * 200  # 51200 bytes, positionally identifiable


class RangeServerTests(unittest.TestCase):
    """The browser VFS reads databases page by page, so range serving is load-bearing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls._directory.name)
        (root / "data").mkdir()
        (root / "data" / "db.sqlite3").write_bytes(BODY)
        with gzip.open(root / "data" / "db.sqlite3.gz", "wb") as handle:
            handle.write(BODY)
        (root / "data" / "manifest.json").write_text('{"schema_version":7}')
        (root / "index.html").write_text("<h1>ok</h1>")

        handler = partial(StaticHandler, directory=str(root))
        cls._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls._server.daemon_threads = True
        cls.port = cls._server.server_address[1]
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls._directory.cleanup()

    def request(self, path: str, headers: dict | None = None, method: str = "GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_serves_requested_byte_range(self) -> None:
        status, headers, body = self.request(
            "/data/db.sqlite3", {"Range": "bytes=100-199"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, BODY[100:200])
        self.assertEqual(headers["Content-Range"], f"bytes 100-199/{len(BODY)}")
        self.assertEqual(headers["Content-Length"], "100")

    def test_range_response_is_never_gzipped(self) -> None:
        # A gzip stream cannot be seeked into, so a range must come from the
        # identity file even when the client accepts gzip.
        status, headers, body = self.request(
            "/data/db.sqlite3", {"Range": "bytes=0-9", "Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 206)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(body, BODY[:10])

    def test_open_ended_and_suffix_ranges(self) -> None:
        _, _, tail = self.request("/data/db.sqlite3", {"Range": f"bytes={len(BODY) - 10}-"})
        self.assertEqual(tail, BODY[-10:])
        _, _, suffix = self.request("/data/db.sqlite3", {"Range": "bytes=-10"})
        self.assertEqual(suffix, BODY[-10:])

    def test_range_end_is_clamped_to_the_file(self) -> None:
        status, _, body = self.request("/data/db.sqlite3", {"Range": "bytes=51100-999999"})
        self.assertEqual(status, 206)
        self.assertEqual(body, BODY[51100:])

    def test_unsatisfiable_range_reports_416(self) -> None:
        status, headers, _ = self.request("/data/db.sqlite3", {"Range": "bytes=999999-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(BODY)}")

    def test_unparseable_range_is_ignored(self) -> None:
        # RFC 7233: a Range header the server cannot parse must be ignored
        # rather than rejected, so the client still gets the whole file.
        for header in ("bytes=abc", "bytes=0-9,20-29", "bytes=5-3"):
            status, _, body = self.request(
                "/data/db.sqlite3", {"Range": header, "Accept-Encoding": "identity"}
            )
            self.assertEqual(status, 200, header)
            self.assertEqual(len(body), len(BODY), header)

    def test_whole_file_negotiates_gzip(self) -> None:
        status, headers, _ = self.request(
            "/data/db.sqlite3", {"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertEqual(headers["Content-Type"], "application/vnd.sqlite3")

    def test_advertises_range_support_and_keep_alive(self) -> None:
        status, headers, _ = self.request("/data/db.sqlite3", {"Accept-Encoding": "identity"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertIn("ETag", headers)

    def test_conditional_request_returns_304(self) -> None:
        _, headers, _ = self.request("/data/db.sqlite3", {"Accept-Encoding": "identity"})
        status, _, body = self.request(
            "/data/db.sqlite3",
            {"If-None-Match": headers["ETag"], "Accept-Encoding": "identity"},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

    def test_manifest_is_never_cached(self) -> None:
        # The manifest is what tells a client every other file changed.
        _, headers, _ = self.request("/data/manifest.json")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_keeps_the_connection_open_between_ranges(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            for offset in range(0, 40960, 4096):
                connection.request(
                    "GET",
                    "/data/db.sqlite3",
                    headers={"Range": f"bytes={offset}-{offset + 4095}"},
                )
                response = connection.getresponse()
                chunk = response.read()
                self.assertEqual(response.status, 206)
                self.assertEqual(chunk, BODY[offset : offset + 4096])
        finally:
            connection.close()

    def test_head_reports_length_without_a_body(self) -> None:
        status, headers, body = self.request(
            "/data/db.sqlite3", {"Accept-Encoding": "identity"}, method="HEAD"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(BODY)))
        self.assertEqual(body, b"")

    def test_missing_file_is_not_found(self) -> None:
        status, _, _ = self.request("/data/absent.sqlite3")
        self.assertEqual(status, 404)

    def test_directory_index_still_served(self) -> None:
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"ok", body)


if __name__ == "__main__":
    unittest.main()
