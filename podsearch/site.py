from __future__ import annotations

import gzip
import hashlib
import os
import pathlib
import shutil
import sqlite3
from collections import defaultdict

from . import storage
from .config import Config


CATALOG_NAME = "catalog.sqlite3"
TRANSCRIPT_DIRECTORY = "transcripts"
LEGACY_DATABASE_NAMES = ("podsearch.sqlite3", "podsearch.sqlite3.gz")


def build_site(config: Config, conn: sqlite3.Connection) -> dict[str, int]:
    public_dir = config.app.public_dir
    data_dir = public_dir / "data"
    vendor_dir = public_dir / "vendor" / "sqlite"
    public_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    vendor_dir.mkdir(parents=True, exist_ok=True)

    source_dir = config.root / "site"
    for name in ("index.html", "styles.css", "app.js", "db-worker.js"):
        shutil.copy2(source_dir / name, public_dir / name)
    for route in ("podcasts", "favorites", "podcast", "episode", "episodes", "search"):
        route_dir = public_dir / route
        route_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / "index.html", route_dir / "index.html")

    sqlite_dist = config.root / "node_modules" / "@sqlite.org" / "sqlite-wasm" / "dist"
    for name in ("index.mjs", "sqlite3.wasm"):
        source = sqlite_dist / name
        if not source.exists():
            raise RuntimeError(f"missing SQLite Wasm asset; run npm install: {source}")
        shutil.copy2(source, vendor_dir / name)

    stats = _build_public_databases(conn, data_dir)
    for legacy_name in LEGACY_DATABASE_NAMES:
        (data_dir / legacy_name).unlink(missing_ok=True)
    return stats


def _build_public_databases(
    source: sqlite3.Connection,
    data_dir: pathlib.Path,
) -> dict[str, int]:
    data_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir = data_dir / TRANSCRIPT_DIRECTORY
    transcript_dir.mkdir(parents=True, exist_ok=True)

    source_shows = _public_shows(source)
    source_episodes = _public_episodes(source)
    generated_at = storage.now_iso()
    existing_shards = _existing_shards(data_dir / CATALOG_NAME)
    grouped_transcripts: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in source_episodes:
        if _has_transcript(row):
            grouped_transcripts[_shard_key(row["published_at"])].append(row)

    shard_stats: list[dict[str, int | str]] = []
    expected_files: set[str] = set()
    for key, rows in sorted(grouped_transcripts.items(), reverse=True):
        output = transcript_dir / f"{key}.sqlite3"
        compressed = transcript_dir / f"{key}.sqlite3.gz"
        version = _shard_version(rows)
        public_path = f"/data/{TRANSCRIPT_DIRECTORY}/{output.name}?v={version}"
        expected_files.update((output.name, compressed.name))
        existing = existing_shards.get(key)
        if (
            existing
            and existing["path"] == public_path
            and output.is_file()
            and compressed.is_file()
        ):
            shard_stats.append(
                {
                    "key": key,
                    "path": public_path,
                    "episode_count": len(rows),
                    "database_bytes": output.stat().st_size,
                    "compressed_bytes": compressed.stat().st_size,
                }
            )
            continue

        temp = transcript_dir / f".{key}.sqlite3.tmp"
        compressed_temp = transcript_dir / f".{key}.sqlite3.gz.tmp"
        temp.unlink(missing_ok=True)
        compressed_temp.unlink(missing_ok=True)
        _build_transcript_shard(rows, temp, key=key, generated_at=generated_at)
        _compress(temp, compressed_temp)
        database_bytes = temp.stat().st_size
        compressed_bytes = compressed_temp.stat().st_size
        os.replace(temp, output)
        os.replace(compressed_temp, compressed)
        shard_stats.append(
            {
                "key": key,
                "path": public_path,
                "episode_count": len(rows),
                "database_bytes": database_bytes,
                "compressed_bytes": compressed_bytes,
            }
        )

    catalog = data_dir / CATALOG_NAME
    catalog_temp = data_dir / f".{CATALOG_NAME}.tmp"
    catalog_gzip = data_dir / f"{CATALOG_NAME}.gz"
    catalog_gzip_temp = data_dir / f".{CATALOG_NAME}.gz.tmp"
    catalog_temp.unlink(missing_ok=True)
    catalog_gzip_temp.unlink(missing_ok=True)
    _build_catalog_database(
        source_shows,
        source_episodes,
        catalog_temp,
        shard_stats=shard_stats,
        generated_at=generated_at,
    )
    _compress(catalog_temp, catalog_gzip_temp)
    catalog_bytes = catalog_temp.stat().st_size
    catalog_compressed_bytes = catalog_gzip_temp.stat().st_size
    os.replace(catalog_temp, catalog)
    os.replace(catalog_gzip_temp, catalog_gzip)

    for path in transcript_dir.iterdir():
        if path.is_file() and not path.name.startswith(".") and path.name not in expected_files:
            path.unlink()

    transcript_database_bytes = sum(int(shard["database_bytes"]) for shard in shard_stats)
    transcript_compressed_bytes = sum(int(shard["compressed_bytes"]) for shard in shard_stats)
    transcript_count = sum(int(shard["episode_count"]) for shard in shard_stats)
    return {
        "public_episodes": len(source_episodes),
        "public_transcripts": transcript_count,
        "public_shows": len(source_shows),
        "transcript_shards": len(shard_stats),
        "catalog_bytes": catalog_bytes,
        "catalog_compressed_bytes": catalog_compressed_bytes,
        "transcript_database_bytes": transcript_database_bytes,
        "transcript_compressed_bytes": transcript_compressed_bytes,
        "database_bytes": catalog_bytes + transcript_database_bytes,
        "compressed_bytes": catalog_compressed_bytes + transcript_compressed_bytes,
    }


def _public_shows(source: sqlite3.Connection) -> list[sqlite3.Row]:
    return source.execute(
        """
        SELECT shows.*
        FROM shows
        WHERE active = 1 AND (ever_top_100 = 1 OR favorite = 1)
        ORDER BY in_top_100 DESC, chart_rank IS NULL, chart_rank, name COLLATE NOCASE
        """
    ).fetchall()


def _public_episodes(source: sqlite3.Connection) -> list[sqlite3.Row]:
    return source.execute(
        """
        SELECT episodes.*, shows.name AS show_name
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE shows.active = 1
          AND (shows.ever_top_100 = 1 OR shows.favorite = 1)
        ORDER BY episodes.id
        """
    ).fetchall()


def _existing_shards(catalog: pathlib.Path) -> dict[str, dict[str, str]]:
    if not catalog.is_file():
        return {}
    try:
        with sqlite3.connect(catalog) as existing:
            return {
                row[0]: {"path": row[1]}
                for row in existing.execute(
                    "SELECT key, path FROM transcript_shards"
                ).fetchall()
            }
    except sqlite3.Error:
        return {}


def _build_catalog_database(
    source_shows: list[sqlite3.Row],
    source_episodes: list[sqlite3.Row],
    output: pathlib.Path,
    *,
    shard_stats: list[dict[str, int | str]],
    generated_at: str,
) -> None:
    public = sqlite3.connect(output)
    try:
        public.executescript(
            """
            PRAGMA page_size = 4096;
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = OFF;

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
              description TEXT,
              episode_url TEXT,
              image_url TEXT,
              published_at TEXT,
              duration TEXT,
              status TEXT NOT NULL,
              has_transcript INTEGER NOT NULL,
              transcript_shard TEXT
            );

            CREATE TABLE transcript_shards (
              key TEXT PRIMARY KEY,
              path TEXT NOT NULL,
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
              description,
              content='episodes',
              content_rowid='id',
              tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        public.executemany(
            """
            INSERT INTO shows (
              id, name, artist, apple_url, artwork_url, chart_rank,
              apple_rank, in_top_100, ever_top_100, favorite, favorite_order,
              homepage_url, description, genres_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["id"],
                    row["name"],
                    row["artist"],
                    row["apple_url"],
                    row["artwork_url"],
                    row["chart_rank"],
                    row["apple_rank"],
                    row["in_top_100"],
                    row["ever_top_100"],
                    row["favorite"],
                    row["favorite_order"],
                    row["homepage_url"],
                    row["description"],
                    row["genres_json"] or "[]",
                )
                for row in source_shows
            ],
        )
        public.executemany(
            """
            INSERT INTO episodes (
              id, show_id, show_name, title, description, episode_url,
              image_url, published_at, duration, status, has_transcript,
              transcript_shard
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["id"],
                    row["show_id"],
                    row["show_name"],
                    row["title"],
                    row["description"],
                    row["episode_url"],
                    row["image_url"],
                    row["published_at"],
                    row["duration"],
                    row["status"],
                    int(_has_transcript(row)),
                    _shard_key(row["published_at"]) if _has_transcript(row) else None,
                )
                for row in source_episodes
            ],
        )
        public.executemany(
            """
            INSERT INTO transcript_shards (
              key, path, episode_count, database_bytes, compressed_bytes
            )
            VALUES (:key, :path, :episode_count, :database_bytes, :compressed_bytes)
            """,
            shard_stats,
        )
        public.execute("INSERT INTO episode_search(episode_search) VALUES('rebuild')")
        transcript_count = sum(int(shard["episode_count"]) for shard in shard_stats)
        public.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("generated_at", generated_at),
                ("episode_count", str(len(source_episodes))),
                ("transcript_count", str(transcript_count)),
                ("show_count", str(len(source_shows))),
                ("shard_count", str(len(shard_stats))),
                ("schema_version", "6"),
            ),
        )
        public.commit()
        public.execute("PRAGMA optimize")
        public.execute("VACUUM")
    finally:
        public.close()


def _build_transcript_shard(
    rows: list[sqlite3.Row],
    output: pathlib.Path,
    *,
    key: str,
    generated_at: str,
) -> None:
    shard = sqlite3.connect(output)
    try:
        shard.executescript(
            """
            PRAGMA page_size = 4096;
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = OFF;

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
        )
        shard.executemany(
            """
            INSERT INTO transcripts (episode_id, title, show_name, transcript_text)
            VALUES (?, ?, ?, ?)
            """,
            [
                (row["id"], row["title"], row["show_name"], row["transcript_text"])
                for row in rows
            ],
        )
        shard.execute("INSERT INTO transcript_search(transcript_search) VALUES('rebuild')")
        shard.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("generated_at", generated_at),
                ("episode_count", str(len(rows))),
                ("schema_version", "1"),
                ("shard_key", key),
            ),
        )
        shard.commit()
        shard.execute("PRAGMA optimize")
        shard.execute("VACUUM")
    finally:
        shard.close()


def _compress(source: pathlib.Path, target: pathlib.Path) -> None:
    with source.open("rb") as input_file, gzip.open(target, "wb", compresslevel=9) as output_file:
        shutil.copyfileobj(input_file, output_file)


def _has_transcript(row: sqlite3.Row) -> bool:
    return row["status"] == "transcribed" and row["transcript_text"] is not None


def _shard_key(published_at: str | None) -> str:
    if published_at and len(published_at) >= 7:
        candidate = published_at[:7]
        if (
            len(candidate) == 7
            and candidate[4] == "-"
            and candidate[:4].isdigit()
            and candidate[5:].isdigit()
            and 1 <= int(candidate[5:]) <= 12
        ):
            return candidate
    return "undated"


def _shard_version(rows: list[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            (
                f"{row['id']}|{row['updated_at']}|"
                f"{len((row['transcript_text'] or '').encode('utf-8'))}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()[:12]
