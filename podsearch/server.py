from __future__ import annotations

import http.server
import pathlib
import urllib.parse
from functools import partial


class StaticHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if getattr(self, "_serving_gzip_database", False):
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if path.endswith((".sqlite3", ".sqlite3.gz")):
            self.send_header("Cache-Control", "public, max-age=300, stale-while-revalidate=60")
        elif path.endswith((".mjs", ".wasm")):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def guess_type(self, path: str) -> str:
        if getattr(self, "_serving_gzip_database", False):
            return "application/vnd.sqlite3"
        if path.endswith(".wasm"):
            return "application/wasm"
        return super().guess_type(path)

    def send_head(self):
        path = urllib.parse.urlparse(self.path).path
        if path.endswith(".sqlite3") and "gzip" in self.headers.get(
            "Accept-Encoding", ""
        ):
            original = self.path
            self.path = f"{path}.gz"
            self._serving_gzip_database = True
            try:
                return super().send_head()
            finally:
                self.path = original
                self._serving_gzip_database = False
        return super().send_head()


def serve(directory: pathlib.Path, host: str, port: int) -> int:
    handler = partial(StaticHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer((host, port), handler) as server:
        print(f"serving=http://{host}:{port}/")
        print(f"directory={directory}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 130
    return 0
