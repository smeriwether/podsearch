from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import time
import unicodedata
from collections import defaultdict
from typing import Callable, Iterable, Iterator

from . import storage
from .config import Config


CATALOG_NAME = "catalog.sqlite3"
DETAILS_NAME = "details.sqlite3"
SEARCH_NAME = "search.sqlite3"
MANIFEST_NAME = "manifest.json"
TRANSCRIPT_DIRECTORY = "transcripts"
STATE_DIRECTORY = "site-state"

SCHEMA_VERSION = 7
SNIPPET_CHARS = 280
PAGE_SIZE = 4096
GZIP_LEVEL = 6
ROW_BATCH = 200
# Fragmentation costs range-request clients real round trips, so compact a
# published file once its freelist grows past this share of the database.
VACUUM_FREELIST_RATIO = 0.2

STATIC_ASSETS = (
    "index.html",
    "styles.css",
    "app.js",
    "db-worker.js",
    "http-vfs.js",
    "favicon.svg",
)
ROUTES = ("podcasts", "favorites", "podcast", "episode", "episodes", "search")
LEGACY_DATA_NAMES = ("podsearch.sqlite3", "podsearch.sqlite3.gz")


def build_site(
    config: Config,
    conn: sqlite3.Connection,
    *,
    minimum_interval_seconds: int = 0,
) -> dict[str, int]:
    run_dir = config.app.state_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "site-build.lock"
    pending_path = run_dir / "site-build.pending"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if minimum_interval_seconds > 0:
                pending_path.touch()
                manifest_path = config.app.public_dir / "data" / MANIFEST_NAME
                if manifest_path.is_file():
                    age = max(0.0, time.time() - manifest_path.stat().st_mtime)
                    if age < minimum_interval_seconds:
                        return {
                            "site_build_deferred": 1,
                            "site_build_wait_seconds": max(
                                1, int(minimum_interval_seconds - age)
                            ),
                        }
            stats = _build_site_unlocked(config, conn)
            pending_path.unlink(missing_ok=True)
            return stats
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _build_site_unlocked(config: Config, conn: sqlite3.Connection) -> dict[str, int]:
    public_dir = config.app.public_dir
    data_dir = public_dir / "data"
    vendor_dir = public_dir / "vendor" / "sqlite"
    state_dir = config.app.state_dir / STATE_DIRECTORY
    for directory in (public_dir, data_dir, vendor_dir, state_dir):
        directory.mkdir(parents=True, exist_ok=True)

    source_dir = config.root / "site"
    asset_version = _asset_version(source_dir)
    for name in STATIC_ASSETS:
        _publish_asset(source_dir / name, public_dir / name, asset_version)
    for route in ROUTES:
        route_dir = public_dir / route
        route_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(public_dir / "index.html", route_dir / "index.html")

    sqlite_dist = config.root / "node_modules" / "@sqlite.org" / "sqlite-wasm" / "dist"
    for name in ("index.mjs", "sqlite3.wasm"):
        source = sqlite_dist / name
        if not source.exists():
            raise RuntimeError(f"missing SQLite Wasm asset; run npm install: {source}")
        shutil.copy2(source, vendor_dir / name)

    stats = _build_public_databases(
        conn, data_dir, state_dir=state_dir, asset_version=asset_version
    )
    for legacy_name in LEGACY_DATA_NAMES:
        (data_dir / legacy_name).unlink(missing_ok=True)
    return stats


# --------------------------------------------------------------------------
# Static assets
# --------------------------------------------------------------------------


def _asset_version(source_dir: pathlib.Path) -> str:
    """One stamp derived from the asset contents themselves.

    Hand-maintained ?v= strings drifted out of sync between index.html and
    app.js, so the cache-busting version is now computed rather than typed.
    """
    digest = hashlib.sha256()
    for name in sorted(STATIC_ASSETS):
        path = source_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:12]


def _publish_asset(source: pathlib.Path, target: pathlib.Path, version: str) -> None:
    text = source.read_text(encoding="utf-8")
    _atomic_write_text(target, text.replace("__ASSET_VERSION__", version))


# --------------------------------------------------------------------------
# Public databases
# --------------------------------------------------------------------------


def _build_public_databases(
    source: sqlite3.Connection,
    data_dir: pathlib.Path,
    *,
    state_dir: pathlib.Path,
    asset_version: str,
) -> dict[str, int]:
    transcript_dir = data_dir / TRANSCRIPT_DIRECTORY
    transcript_dir.mkdir(parents=True, exist_ok=True)
    generated_at = storage.now_iso()

    shows = _public_shows(source)
    episodes = _public_episode_index(source)
    transcripts = _transcript_index(source)

    shard_keys = {int(row["id"]): _shard_key(row["published_at"]) for row in transcripts}
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in transcripts:
        grouped[shard_keys[int(row["id"])]].append(row)

    shard_stats = [
        _sync_shard(
            source,
            transcript_dir / f"{key}.sqlite3",
            state_dir=state_dir,
            key=key,
            rows=rows,
            generated_at=generated_at,
        )
        for key, rows in sorted(grouped.items(), reverse=True)
    ]
    _prune_shards(transcript_dir, {str(shard["key"]) for shard in shard_stats})

    search_stats = _sync_search_index(
        source,
        data_dir / SEARCH_NAME,
        state_dir=state_dir,
        transcripts=transcripts,
        generated_at=generated_at,
    )
    details_stats = _sync_details(
        source,
        data_dir / DETAILS_NAME,
        state_dir=state_dir,
        episodes=episodes,
        generated_at=generated_at,
    )
    catalog_stats = _sync_catalog(
        data_dir / CATALOG_NAME,
        state_dir=state_dir,
        shows=shows,
        episodes=episodes,
        shard_keys=shard_keys,
        shard_stats=shard_stats,
        generated_at=generated_at,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "assets": asset_version,
        "catalog": _manifest_entry(CATALOG_NAME, catalog_stats),
        "details": _manifest_entry(DETAILS_NAME, details_stats),
        "search": _manifest_entry(SEARCH_NAME, search_stats),
        "shards": [
            {
                **_manifest_entry(
                    f"{TRANSCRIPT_DIRECTORY}/{shard['key']}.sqlite3", shard
                ),
                "key": shard["key"],
                "episode_count": shard["episode_count"],
            }
            for shard in shard_stats
        ],
        "counts": {
            "episodes": len(episodes),
            "shows": len(shows),
            "transcripts": len(transcripts),
            "shards": len(shard_stats),
        },
    }
    _atomic_write_text(
        data_dir / MANIFEST_NAME,
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
    )

    transcript_bytes = sum(int(shard["bytes"]) for shard in shard_stats)
    transcript_compressed = sum(int(shard["compressed_bytes"]) for shard in shard_stats)
    return {
        "public_shows": len(shows),
        "public_episodes": len(episodes),
        "public_transcripts": len(transcripts),
        "transcript_shards": len(shard_stats),
        "rebuilt_databases": sum(
            int(entry["rebuilt"])
            for entry in (catalog_stats, details_stats, search_stats, *shard_stats)
        ),
        "catalog_bytes": catalog_stats["bytes"],
        "details_bytes": details_stats["bytes"],
        "search_bytes": search_stats["bytes"],
        "transcript_database_bytes": transcript_bytes,
        "transcript_compressed_bytes": transcript_compressed,
        "first_load_bytes": catalog_stats["compressed_bytes"],
        "database_bytes": (
            int(catalog_stats["bytes"])
            + int(details_stats["bytes"])
            + int(search_stats["bytes"])
            + transcript_bytes
        ),
    }


def _manifest_entry(name: str, stats: dict) -> dict:
    return {
        "path": f"/data/{name}?v={stats['version']}",
        "version": stats["version"],
        "bytes": stats["bytes"],
        "compressed_bytes": stats["compressed_bytes"],
    }


# --------------------------------------------------------------------------
# Source queries
#
# None of these pull transcript_text or description for the whole archive; the
# large columns are streamed in batches, and only for the rows being rebuilt.
# --------------------------------------------------------------------------


def _public_shows(source: sqlite3.Connection) -> list[sqlite3.Row]:
    return source.execute(
        """
        SELECT id, name, artist, apple_url, homepage_url, artwork_url, description,
               genres_json, chart_rank, apple_rank, in_top_100, ever_top_100,
               favorite, favorite_order
        FROM shows
        WHERE active = 1 AND (ever_top_100 = 1 OR favorite = 1)
        ORDER BY in_top_100 DESC, chart_rank IS NULL, chart_rank, name COLLATE NOCASE
        """
    ).fetchall()


def _public_episode_index(source: sqlite3.Connection) -> list[sqlite3.Row]:
    return source.execute(
        f"""
        SELECT episodes.id, episodes.show_id, shows.name AS show_name,
               episodes.title, episodes.published_at, episodes.duration,
               episodes.updated_at, episodes.episode_url, episodes.image_url,
               substr(episodes.description, 1, {SNIPPET_CHARS + 40}) AS snippet_source,
               length(episodes.description) AS description_length,
               CASE
                 WHEN episodes.status = 'transcribed'
                  AND episodes.transcript_text IS NOT NULL THEN 1
                 ELSE 0
               END AS has_transcript
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE shows.active = 1 AND (shows.ever_top_100 = 1 OR shows.favorite = 1)
        ORDER BY episodes.id
        """
    ).fetchall()


def _transcript_index(source: sqlite3.Connection) -> list[sqlite3.Row]:
    return source.execute(
        """
        SELECT episodes.id, episodes.published_at, episodes.transcript_sha256,
               episodes.title, shows.name AS show_name
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE shows.active = 1 AND (shows.ever_top_100 = 1 OR shows.favorite = 1)
          AND episodes.status = 'transcribed'
          AND episodes.transcript_text IS NOT NULL
        ORDER BY episodes.id
        """
    ).fetchall()


def _stream_rows(
    source: sqlite3.Connection,
    episode_ids: list[int],
    sql: str,
) -> Iterator[sqlite3.Row]:
    for batch in _batched(episode_ids, ROW_BATCH):
        placeholders = ",".join("?" for _ in batch)
        yield from source.execute(sql.format(placeholders=placeholders), batch)


TRANSCRIPT_ROW_SQL = """
    SELECT episodes.id, episodes.title, shows.name AS show_name,
           episodes.transcript_text
    FROM episodes
    JOIN shows ON shows.id = episodes.show_id
    WHERE episodes.id IN ({placeholders})
"""

DESCRIPTION_ROW_SQL = """
    SELECT id, description FROM episodes WHERE id IN ({placeholders})
"""


def _description_digests(source: sqlite3.Connection) -> dict[int, str]:
    """Hash each description without ever holding them all in memory.

    updated_at is useless as a fingerprint here: writing a transcript bumps it,
    which would rebuild the whole details database on every backfill pass even
    though no description changed. Hashing the text is a cheap streaming scan
    and is exactly right.
    """
    digests: dict[int, str] = {}
    cursor = source.execute(
        """
        SELECT episodes.id, episodes.description
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE shows.active = 1 AND (shows.ever_top_100 = 1 OR shows.favorite = 1)
          AND episodes.description IS NOT NULL AND episodes.description != ''
        """
    )
    for row in cursor:
        digests[int(row["id"])] = hashlib.sha256(
            str(row["description"]).encode("utf-8")
        ).hexdigest()[:16]
    return digests


# --------------------------------------------------------------------------
# Catalog: the one database downloaded whole, so it holds only what browsing
# and result lists need. Descriptions, URLs and transcripts live elsewhere.
# --------------------------------------------------------------------------


CATALOG_SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE shows (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  artist TEXT,
  apple_url TEXT,
  homepage_url TEXT,
  artwork_url TEXT,
  description TEXT,
  search_text TEXT NOT NULL,
  genres_json TEXT NOT NULL,
  chart_rank INTEGER,
  apple_rank INTEGER,
  in_top_100 INTEGER NOT NULL,
  ever_top_100 INTEGER NOT NULL,
  favorite INTEGER NOT NULL,
  favorite_order INTEGER
);

CREATE TABLE episodes (
  id INTEGER PRIMARY KEY,
  show_id INTEGER NOT NULL REFERENCES shows(id),
  show_name TEXT NOT NULL,
  title TEXT NOT NULL,
  snippet TEXT NOT NULL,
  published_at TEXT,
  duration TEXT,
  has_transcript INTEGER NOT NULL,
  has_detail INTEGER NOT NULL,
  transcript_shard TEXT
);

CREATE TABLE transcript_shards (
  key TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  version TEXT NOT NULL,
  episode_count INTEGER NOT NULL,
  database_bytes INTEGER NOT NULL,
  compressed_bytes INTEGER NOT NULL
) WITHOUT ROWID;

CREATE INDEX idx_public_episodes_show_date
  ON episodes(show_id, published_at DESC);
CREATE INDEX idx_public_episodes_date
  ON episodes(published_at DESC);
CREATE INDEX idx_public_episodes_transcript_date
  ON episodes(has_transcript, published_at DESC);

CREATE VIRTUAL TABLE episode_search USING fts5(
  title,
  show_name,
  snippet,
  content='episodes',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
"""


def _sync_catalog(
    output: pathlib.Path,
    *,
    state_dir: pathlib.Path,
    shows: list[sqlite3.Row],
    episodes: list[sqlite3.Row],
    shard_keys: dict[int, str],
    shard_stats: list[dict],
    generated_at: str,
) -> dict:
    desired: dict[int, tuple[str, str]] = {}
    payloads: dict[int, tuple] = {}
    for row in episodes:
        episode_id = int(row["id"])
        snippet = _snippet(row["snippet_source"])
        has_detail = bool(
            row["description_length"] or row["episode_url"] or row["image_url"]
        )
        payload = (
            episode_id,
            int(row["show_id"]),
            str(row["show_name"]),
            str(row["title"]),
            snippet,
            row["published_at"],
            row["duration"],
            int(row["has_transcript"]),
            int(has_detail),
            shard_keys.get(episode_id),
        )
        payloads[episode_id] = payload
        desired[episode_id] = (
            _digest(*(str(value) for value in payload)),
            _digest(str(row["title"]), str(row["show_name"]), snippet),
        )

    version = _version(
        "catalog",
        ((key, value[0]) for key, value in sorted(desired.items())),
        extra=[str(shard["version"]) for shard in shard_stats],
    )

    def populate(db: sqlite3.Connection, existing: dict[int, object]) -> None:
        db.execute("DELETE FROM shows")
        db.executemany(
            """
            INSERT INTO shows (
              id, name, artist, apple_url, homepage_url, artwork_url, description,
              search_text, genres_json, chart_rank, apple_rank, in_top_100,
              ever_top_100, favorite, favorite_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["id"],
                    row["name"],
                    row["artist"],
                    row["apple_url"],
                    row["homepage_url"],
                    row["artwork_url"],
                    row["description"],
                    fold_search_text(
                        f"{row['name']} {row['artist'] or ''} {row['description'] or ''}"
                    ),
                    row["genres_json"] or "[]",
                    row["chart_rank"],
                    row["apple_rank"],
                    row["in_top_100"],
                    row["ever_top_100"],
                    row["favorite"],
                    row["favorite_order"],
                )
                for row in shows
            ],
        )

        removed = [key for key in existing if key not in desired]
        changed = [key for key, value in desired.items() if existing.get(key) != value]
        # episode_search only mirrors title/show_name/snippet, so a backfill run
        # that merely flips has_transcript never has to touch the FTS index.
        text_dirty = [
            key
            for key in changed
            if _second(existing.get(key)) != desired[key][1]
        ]
        _fts_delete(db, removed + text_dirty)
        for batch in _batched(removed + changed, ROW_BATCH):
            placeholders = ",".join("?" for _ in batch)
            db.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", batch)
        db.executemany(
            """
            INSERT INTO episodes (
              id, show_id, show_name, title, snippet, published_at, duration,
              has_transcript, has_detail, transcript_shard
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [payloads[key] for key in changed],
        )
        for batch in _batched(text_dirty, ROW_BATCH):
            placeholders = ",".join("?" for _ in batch)
            db.execute(
                f"""
                INSERT INTO episode_search(rowid, title, show_name, snippet)
                SELECT id, title, show_name, snippet
                FROM episodes WHERE id IN ({placeholders})
                """,
                batch,
            )

        db.execute("DELETE FROM transcript_shards")
        db.executemany(
            """
            INSERT INTO transcript_shards (
              key, path, version, episode_count, database_bytes, compressed_bytes
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    shard["key"],
                    f"/data/{TRANSCRIPT_DIRECTORY}/{shard['key']}.sqlite3?v={shard['version']}",
                    shard["version"],
                    shard["episode_count"],
                    shard["bytes"],
                    shard["compressed_bytes"],
                )
                for shard in shard_stats
            ],
        )
        _write_meta(
            db,
            generated_at=generated_at,
            version=version,
            extra={
                "episode_count": str(len(episodes)),
                "show_count": str(len(shows)),
                "transcript_count": str(
                    sum(int(shard["episode_count"]) for shard in shard_stats)
                ),
                "shard_count": str(len(shard_stats)),
            },
        )

    return _sync_database(
        output,
        state_dir=state_dir,
        schema=CATALOG_SCHEMA,
        version=version,
        desired=desired,
        populate=populate,
        compress=True,
    )


def _fts_delete(db: sqlite3.Connection, episode_ids: list[int]) -> None:
    """Retract rows from the external-content FTS index before their content goes."""
    for batch in _batched(episode_ids, ROW_BATCH):
        placeholders = ",".join("?" for _ in batch)
        db.execute(
            f"""
            INSERT INTO episode_search(episode_search, rowid, title, show_name, snippet)
            SELECT 'delete', id, title, show_name, snippet
            FROM episodes WHERE id IN ({placeholders})
            """,
            batch,
        )


# --------------------------------------------------------------------------
# Details: descriptions and per-episode links, read one row at a time over
# HTTP ranges rather than downloaded.
# --------------------------------------------------------------------------


DETAILS_SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE details (
  episode_id INTEGER PRIMARY KEY,
  description TEXT,
  episode_url TEXT,
  image_url TEXT
);
"""


def _sync_details(
    source: sqlite3.Connection,
    output: pathlib.Path,
    *,
    state_dir: pathlib.Path,
    episodes: list[sqlite3.Row],
    generated_at: str,
) -> dict:
    description_digests = _description_digests(source)
    links: dict[int, tuple] = {}
    desired: dict[int, str] = {}
    for row in episodes:
        if not (row["description_length"] or row["episode_url"] or row["image_url"]):
            continue
        episode_id = int(row["id"])
        links[episode_id] = (row["episode_url"], row["image_url"])
        desired[episode_id] = _digest(
            description_digests.get(episode_id, ""),
            str(row["episode_url"]),
            str(row["image_url"]),
        )

    version = _version("details", sorted(desired.items()))

    def populate(db: sqlite3.Connection, existing: dict[int, object]) -> None:
        removed = [key for key in existing if key not in desired]
        changed = [key for key, value in desired.items() if existing.get(key) != value]
        for batch in _batched(removed + changed, ROW_BATCH):
            placeholders = ",".join("?" for _ in batch)
            db.execute(
                f"DELETE FROM details WHERE episode_id IN ({placeholders})", batch
            )
        pending: list[tuple] = []
        for row in _stream_rows(source, changed, DESCRIPTION_ROW_SQL):
            episode_id = int(row["id"])
            episode_url, image_url = links[episode_id]
            pending.append((episode_id, row["description"], episode_url, image_url))
            if len(pending) >= ROW_BATCH:
                _insert_details(db, pending)
                pending = []
        _insert_details(db, pending)
        _write_meta(
            db,
            generated_at=generated_at,
            version=version,
            extra={"detail_count": str(len(desired))},
        )

    return _sync_database(
        output,
        state_dir=state_dir,
        schema=DETAILS_SCHEMA,
        version=version,
        desired=desired,
        populate=populate,
        compress=True,
    )


def _insert_details(db: sqlite3.Connection, pending: list[tuple]) -> None:
    if pending:
        db.executemany(
            """
            INSERT OR REPLACE INTO details (
              episode_id, description, episode_url, image_url
            ) VALUES (?, ?, ?, ?)
            """,
            pending,
        )


# --------------------------------------------------------------------------
# Global search index: one contentless FTS table covering every transcript, so
# a search is a single b-tree descent instead of a fan-out across shards.
# --------------------------------------------------------------------------


SEARCH_SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE VIRTUAL TABLE transcript_search USING fts5(
  title,
  show_name,
  transcript_text,
  content='',
  tokenize='unicode61 remove_diacritics 2'
);
"""


def _sync_search_index(
    source: sqlite3.Connection,
    output: pathlib.Path,
    *,
    state_dir: pathlib.Path,
    transcripts: list[sqlite3.Row],
    generated_at: str,
) -> dict:
    desired = {
        int(row["id"]): _digest(
            str(row["transcript_sha256"]),
            str(row["title"]),
            str(row["show_name"]),
        )
        for row in transcripts
    }
    version = _version("search", sorted(desired.items()))

    def populate(db: sqlite3.Connection, existing: dict[int, object]) -> None:
        added = [key for key, value in desired.items() if existing.get(key) != value]
        pending: list[tuple] = []
        for row in _stream_rows(source, added, TRANSCRIPT_ROW_SQL):
            pending.append(
                (
                    int(row["id"]),
                    str(row["title"]),
                    str(row["show_name"]),
                    str(row["transcript_text"]),
                )
            )
            if len(pending) >= ROW_BATCH:
                _insert_search_rows(db, pending)
                pending = []
        _insert_search_rows(db, pending)
        _write_meta(
            db,
            generated_at=generated_at,
            version=version,
            extra={"transcript_count": str(len(desired))},
        )

    # A contentless FTS5 table cannot retract a row without its original text,
    # so a removal or re-transcription restarts from empty. The backfill only
    # ever appends, which stays on the cheap incremental path.
    return _sync_database(
        output,
        state_dir=state_dir,
        schema=SEARCH_SCHEMA,
        version=version,
        desired=desired,
        populate=populate,
        compress=True,
        rebuild_when=lambda existing: any(
            desired.get(key) != value for key, value in existing.items()
        ),
    )


def _insert_search_rows(db: sqlite3.Connection, pending: list[tuple]) -> None:
    if pending:
        db.executemany(
            """
            INSERT INTO transcript_search(rowid, title, show_name, transcript_text)
            VALUES (?, ?, ?, ?)
            """,
            pending,
        )


# --------------------------------------------------------------------------
# Monthly transcript shards: full text plus a snippet-capable FTS index
# --------------------------------------------------------------------------


SHARD_SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE transcripts (
  episode_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  show_name TEXT NOT NULL,
  transcript_text TEXT NOT NULL
);

CREATE VIRTUAL TABLE transcript_search USING fts5(
  title,
  show_name,
  transcript_text,
  content='transcripts',
  content_rowid='episode_id',
  tokenize='unicode61 remove_diacritics 2'
);
"""


def _sync_shard(
    source: sqlite3.Connection,
    output: pathlib.Path,
    *,
    state_dir: pathlib.Path,
    key: str,
    rows: list[sqlite3.Row],
    generated_at: str,
) -> dict:
    desired = {
        int(row["id"]): _digest(
            str(row["transcript_sha256"]),
            str(row["title"]),
            str(row["show_name"]),
        )
        for row in rows
    }
    version = _version(f"shard:{key}", sorted(desired.items()))

    def populate(db: sqlite3.Connection, existing: dict[int, object]) -> None:
        removed = [k for k in existing if k not in desired]
        changed = [k for k, value in desired.items() if existing.get(k) != value]
        for batch in _batched(removed + changed, ROW_BATCH):
            placeholders = ",".join("?" for _ in batch)
            db.execute(
                f"""
                INSERT INTO transcript_search(
                  transcript_search, rowid, title, show_name, transcript_text
                )
                SELECT 'delete', episode_id, title, show_name, transcript_text
                FROM transcripts WHERE episode_id IN ({placeholders})
                """,
                batch,
            )
            db.execute(
                f"DELETE FROM transcripts WHERE episode_id IN ({placeholders})", batch
            )
        pending: list[tuple] = []
        for row in _stream_rows(source, changed, TRANSCRIPT_ROW_SQL):
            pending.append(
                (
                    int(row["id"]),
                    str(row["title"]),
                    str(row["show_name"]),
                    str(row["transcript_text"]),
                )
            )
            if len(pending) >= ROW_BATCH:
                _insert_shard_rows(db, pending)
                pending = []
        _insert_shard_rows(db, pending)
        _write_meta(
            db,
            generated_at=generated_at,
            version=version,
            extra={"shard_key": key, "episode_count": str(len(desired))},
        )

    stats = _sync_database(
        output,
        state_dir=state_dir,
        schema=SHARD_SCHEMA,
        version=version,
        desired=desired,
        populate=populate,
        compress=True,
    )
    stats["key"] = key
    stats["episode_count"] = len(desired)
    return stats


def _insert_shard_rows(db: sqlite3.Connection, pending: list[tuple]) -> None:
    if not pending:
        return
    db.executemany(
        """
        INSERT INTO transcripts (episode_id, title, show_name, transcript_text)
        VALUES (?, ?, ?, ?)
        """,
        pending,
    )
    placeholders = ",".join("?" for _ in pending)
    db.execute(
        f"""
        INSERT INTO transcript_search(rowid, title, show_name, transcript_text)
        SELECT episode_id, title, show_name, transcript_text
        FROM transcripts WHERE episode_id IN ({placeholders})
        """,
        [item[0] for item in pending],
    )


def _prune_shards(transcript_dir: pathlib.Path, keys: set[str]) -> None:
    expected = set()
    for key in keys:
        expected.update((f"{key}.sqlite3", f"{key}.sqlite3.gz"))
    for path in transcript_dir.iterdir():
        if path.is_file() and not path.name.startswith(".") and path.name not in expected:
            path.unlink()


# --------------------------------------------------------------------------
# Shared incremental-publish machinery
#
# Per-row build fingerprints live in a sidecar database under var/, never in
# the published file: visitors should not download bookkeeping. The sidecar
# records the artifact version it describes, so a mismatch (a hand-deleted
# public/, a partial copy) safely forces a full rebuild.
# --------------------------------------------------------------------------


def _sync_database(
    output: pathlib.Path,
    *,
    state_dir: pathlib.Path,
    schema: str,
    version: str,
    desired: dict[int, object],
    populate: Callable[[sqlite3.Connection, dict[int, object]], None],
    compress: bool,
    rebuild_when: Callable[[dict[int, object]], bool] | None = None,
) -> dict:
    compressed_path = output.with_name(f"{output.name}.gz")
    state_path = state_dir / f"{output.name}.state"
    published_version = _stored_version(output)

    if (
        output.is_file()
        and (not compress or compressed_path.is_file())
        and published_version == version
    ):
        return {
            "version": version,
            "bytes": output.stat().st_size,
            "compressed_bytes": compressed_path.stat().st_size if compress else 0,
            "rebuilt": 0,
        }

    existing = _read_build_state(state_path, published_version)
    reuse = (
        output.is_file()
        and published_version is not None
        and (rebuild_when is None or not rebuild_when(existing))
    )
    if not reuse:
        existing = {}

    temp = output.with_name(f".{output.name}.tmp")
    temp.unlink(missing_ok=True)
    if reuse:
        shutil.copyfile(output, temp)

    db = sqlite3.connect(temp, isolation_level=None)
    try:
        db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        db.execute("PRAGMA journal_mode = DELETE")
        db.execute("PRAGMA synchronous = OFF")
        if not reuse:
            db.executescript(schema)
        db.execute("BEGIN")
        try:
            populate(db, existing)
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        db.execute("PRAGMA optimize")
        if not reuse or _should_vacuum(db):
            db.execute("VACUUM")
    finally:
        db.close()

    size = temp.stat().st_size
    compressed_bytes = 0
    if compress:
        compressed_temp = output.with_name(f".{output.name}.gz.tmp")
        compressed_temp.unlink(missing_ok=True)
        _compress(temp, compressed_temp)
        compressed_bytes = compressed_temp.stat().st_size
        # Publish the compressed copy first so a client that sees the identity
        # file always finds a matching .gz beside it.
        os.replace(compressed_temp, compressed_path)
    os.replace(temp, output)
    _write_build_state(state_path, version, desired)
    return {
        "version": version,
        "bytes": size,
        "compressed_bytes": compressed_bytes,
        "rebuilt": 1,
    }


def _should_vacuum(db: sqlite3.Connection) -> bool:
    free = int(db.execute("PRAGMA freelist_count").fetchone()[0])
    total = int(db.execute("PRAGMA page_count").fetchone()[0])
    return bool(total) and free / total > VACUUM_FREELIST_RATIO


def _read_build_state(
    state_path: pathlib.Path,
    published_version: str | None,
) -> dict[int, object]:
    if published_version is None or not state_path.is_file():
        return {}
    db = None
    try:
        db = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        recorded = db.execute("SELECT value FROM state_meta WHERE key='version'").fetchone()
        if not recorded or str(recorded[0]) != published_version:
            return {}
        rows = db.execute("SELECT id, fingerprint, extra FROM build_state").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if db is not None:
            db.close()
    return {
        int(row[0]): (str(row[1]), str(row[2])) if row[2] is not None else str(row[1])
        for row in rows
    }


def _write_build_state(
    state_path: pathlib.Path,
    version: str,
    desired: dict[int, object],
) -> None:
    temp = state_path.with_name(f".{state_path.name}.tmp")
    temp.unlink(missing_ok=True)
    db = sqlite3.connect(temp, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.executescript(
            """
            CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE build_state (
              id INTEGER PRIMARY KEY,
              fingerprint TEXT NOT NULL,
              extra TEXT
            );
            """
        )
        db.execute("BEGIN")
        db.execute("INSERT INTO state_meta (key, value) VALUES ('version', ?)", (version,))
        db.executemany(
            "INSERT INTO build_state (id, fingerprint, extra) VALUES (?, ?, ?)",
            [
                (key, value[0], value[1])
                if isinstance(value, tuple)
                else (key, value, None)
                for key, value in desired.items()
            ],
        )
        db.execute("COMMIT")
    finally:
        db.close()
    os.replace(temp, state_path)


def _stored_version(path: pathlib.Path) -> str | None:
    if not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        if db is not None:
            db.close()
    return str(row[0]) if row else None


def _write_meta(
    db: sqlite3.Connection,
    *,
    generated_at: str,
    version: str,
    extra: dict[str, str],
) -> None:
    db.execute("DELETE FROM meta")
    db.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("generated_at", generated_at),
            ("version", version),
            ("schema_version", str(SCHEMA_VERSION)),
            *sorted(extra.items()),
        ],
    )


def _compress(source: pathlib.Path, target: pathlib.Path) -> None:
    # mtime=0 keeps the gzip byte-identical when the input has not changed.
    with source.open("rb") as input_file, target.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_output, compresslevel=GZIP_LEVEL, mtime=0
        ) as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def fold_search_text(value: str | None) -> str:
    """Casefold and strip diacritics so LIKE matching is Unicode-aware.

    SQLite's built-in lower() only folds ASCII, so 'ÉLÉPHANT' never matched a
    lowercase query. Folding at build time keeps the browser-side query simple
    and the comparison symmetric.

    The browser folds the query the same way with String.toLowerCase(), so
    str.lower() is used here rather than str.casefold() to keep both sides
    character-for-character symmetric.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFKC", stripped).lower()


def _snippet(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) <= SNIPPET_CHARS:
        return text
    clipped = text[:SNIPPET_CHARS]
    head, separator, _ = clipped.rpartition(" ")
    return f"{head if separator else clipped}…"


def _second(value: object) -> object:
    return value[1] if isinstance(value, tuple) else None


def _digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:16]


def _version(
    label: str,
    items: Iterable[tuple[int, str]],
    extra: Iterable[str] = (),
) -> str:
    digest = hashlib.sha256()
    digest.update(f"{label}|{SCHEMA_VERSION}".encode("utf-8"))
    for key, value in items:
        digest.update(f"{key}:{value}\n".encode("utf-8"))
    for value in extra:
        digest.update(f"+{value}\n".encode("utf-8"))
    return digest.hexdigest()[:12]


def _batched(values: Iterable[int], size: int) -> Iterator[list[int]]:
    batch: list[int] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _shard_key(published_at: str | None) -> str:
    if published_at and len(published_at) >= 7:
        candidate = published_at[:7]
        if (
            candidate[4] == "-"
            and candidate[:4].isdigit()
            and candidate[5:].isdigit()
            and 1 <= int(candidate[5:]) <= 12
        ):
            return candidate
    return "undated"
