from __future__ import annotations

import email.utils
import http.server
import os
import pathlib
import re
import urllib.parse
from functools import partial


RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
COPY_BLOCK_SIZE = 256 * 1024

# RFC 7233 draws a line the status codes depend on: a Range header the server
# does not understand must be ignored and answered with the whole file, while a
# well-formed range that falls outside the file must be answered with 416.
RANGE_IGNORED = object()
RANGE_UNSATISFIABLE = object()

DATABASE_SUFFIXES = (".sqlite3", ".sqlite3.gz")
IMMUTABLE_SUFFIXES = (".mjs", ".wasm")


class _RangeReader:
    """A bounded reader so copyfile() stops at the end of the requested range."""

    def __init__(self, stream, start: int, length: int) -> None:
        self._stream = stream
        self._remaining = length
        stream.seek(start)

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        wanted = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._stream.read(wanted)
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._stream.close()


class StaticHandler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1 keeps connections alive, which matters a great deal for the
    # range requests the browser VFS issues while walking a SQLite b-tree.
    protocol_version = "HTTP/1.1"
    server_version = "podsearch"
    sys_version = ""
    timeout = 30

    def end_headers(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/manifest.json"):
            # The manifest is the freshness signal for every other file, so it
            # must never be served from a cache.
            self.send_header("Cache-Control", "no-store")
        elif path.endswith(DATABASE_SUFFIXES):
            self.send_header("Cache-Control", "public, max-age=300, stale-while-revalidate=60")
        elif path.endswith(IMMUTABLE_SUFFIXES):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def guess_type(self, path: str) -> str:
        text = str(path)
        if text.endswith(".wasm"):
            return "application/wasm"
        if text.endswith(".sqlite3"):
            return "application/vnd.sqlite3"
        return super().guess_type(path)

    def send_head(self):
        local = pathlib.Path(self.translate_path(self.path))
        if local.is_dir():
            # Directory redirects and index.html lookup stay in the stdlib.
            return super().send_head()

        url_path = urllib.parse.urlparse(self.path).path
        range_header = self.headers.get("Range")
        serve_path, encoding = self._negotiate(local, range_header)
        if serve_path is None:
            self.send_error(404, "File not found")
            return None

        try:
            stream = serve_path.open("rb")
            info = os.fstat(stream.fileno())
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = info.st_size
        etag = f'"{info.st_mtime_ns:x}-{size:x}"'
        last_modified = self.date_time_string(info.st_mtime)
        content_type = self.guess_type(url_path)

        if self._is_unmodified(etag, info.st_mtime):
            stream.close()
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            if encoding:
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return None

        if range_header is None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            if encoding:
                self.send_header("Content-Encoding", encoding)
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return stream

        span = self._parse_range(range_header, size)
        if span is RANGE_UNSATISFIABLE:
            stream.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return None
        if span is RANGE_IGNORED:
            # Unsupported or malformed syntax: answer with the entire file.
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            return stream

        start, end = span
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.end_headers()
        return _RangeReader(stream, start, length)

    def copyfile(self, source, outputfile) -> None:
        while True:
            chunk = source.read(COPY_BLOCK_SIZE)
            if not chunk:
                break
            outputfile.write(chunk)

    def _negotiate(
        self,
        local: pathlib.Path,
        range_header: str | None,
    ) -> tuple[pathlib.Path | None, str | None]:
        """Pick the pre-compressed sibling only when the whole file is wanted.

        A gzip stream cannot be seeked into, so a range request is always
        answered from the identity file.
        """
        if range_header is None and self._accepts_gzip():
            compressed = local.with_name(f"{local.name}.gz")
            if compressed.is_file():
                return compressed, "gzip"
        if local.is_file():
            return local, None
        return None, None

    def _accepts_gzip(self) -> bool:
        accepted = self.headers.get("Accept-Encoding", "")
        return any(
            part.split(";", 1)[0].strip() == "gzip" for part in accepted.split(",")
        )

    def _is_unmodified(self, etag: str, mtime: float) -> bool:
        if self.headers.get("If-Range"):
            return False
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match:
            candidates = {value.strip() for value in if_none_match.split(",")}
            return "*" in candidates or etag in candidates
        if_modified_since = self.headers.get("If-Modified-Since")
        if not if_modified_since:
            return False
        try:
            since = email.utils.parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError, IndexError):
            return False
        if since is None:
            return False
        return int(mtime) <= int(since.timestamp())

    @staticmethod
    def _parse_range(header: str, size: int):
        """Parse a single byte range.

        Multipart ranges are deliberately unsupported; like any range syntax we
        do not handle, they are reported as RANGE_IGNORED so the caller answers
        with the whole file rather than a 416.
        """
        match = RANGE_PATTERN.match(header.strip())
        if not match:
            return RANGE_IGNORED
        first, last = match.group(1), match.group(2)
        if not first and not last:
            return RANGE_IGNORED
        if not first:
            # Suffix range: the final N bytes.
            length = int(last)
            if length <= 0:
                return RANGE_UNSATISFIABLE
            return max(0, size - length), size - 1
        start = int(first)
        if last and int(last) < start:
            # An inverted range makes the whole header invalid.
            return RANGE_IGNORED
        if start >= size:
            return RANGE_UNSATISFIABLE
        end = size - 1 if not last else min(int(last), size - 1)
        return start, end

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # Serving a page-at-a-time database makes per-request visibility useful
        # when diagnosing how much of a file a query actually touched.
        if os.environ.get("PODSEARCH_ACCESS_LOG"):
            super().log_message(
                "%s range=%s", format % args, self.headers.get("Range", "-")
            )


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(directory: pathlib.Path, host: str, port: int) -> int:
    handler = partial(StaticHandler, directory=str(directory))
    with _Server((host, port), handler) as server:
        print(f"serving=http://{host}:{port}/")
        print(f"directory={directory}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 130
    return 0
