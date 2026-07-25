from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from typing import Any, Iterable

from .config import Config


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect(config: Config) -> sqlite3.Connection:
    config.app.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.app.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE IF NOT EXISTS shows (
          id INTEGER PRIMARY KEY,
          apple_id TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          artist TEXT,
          apple_url TEXT,
          feed_url TEXT,
          artwork_url TEXT,
          chart_rank INTEGER,
          apple_rank INTEGER,
          in_top_100 INTEGER NOT NULL DEFAULT 0,
          ever_top_100 INTEGER NOT NULL DEFAULT 0,
          favorite INTEGER NOT NULL DEFAULT 0,
          favorite_order INTEGER,
          active INTEGER NOT NULL DEFAULT 1,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_shows_chart
          ON shows(in_top_100 DESC, chart_rank ASC);

        CREATE TABLE IF NOT EXISTS episodes (
          id INTEGER PRIMARY KEY,
          show_id INTEGER NOT NULL REFERENCES shows(id),
          guid TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT,
          episode_url TEXT,
          audio_url TEXT,
          audio_type TEXT,
          image_url TEXT,
          published_at TEXT,
          duration TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          error_message TEXT,
          transcript_path TEXT,
          transcript_text TEXT,
          transcribed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(show_id, guid)
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_status
          ON episodes(status, published_at);
        CREATE INDEX IF NOT EXISTS idx_episodes_published
          ON episodes(published_at DESC);

        CREATE TABLE IF NOT EXISTS chart_snapshots (
          captured_at TEXT NOT NULL,
          country TEXT NOT NULL,
          rank INTEGER NOT NULL,
          apple_id TEXT NOT NULL,
          PRIMARY KEY(captured_at, country, rank)
        );

        CREATE TABLE IF NOT EXISTS pipeline_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(conn, "shows", "description", "TEXT")
    _ensure_column(conn, "shows", "homepage_url", "TEXT")
    _ensure_column(conn, "shows", "genres_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "shows", "ever_top_100", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "shows", "apple_rank", "INTEGER")
    _ensure_column(conn, "shows", "favorite_order", "INTEGER")
    conn.execute(
        """
        UPDATE shows
        SET ever_top_100 = 1
        WHERE in_top_100 = 1
           OR apple_id IN (SELECT apple_id FROM chart_snapshots)
        """
    )
    conn.execute(
        """
        UPDATE shows
        SET chart_rank = (
          SELECT chart_snapshots.rank
          FROM chart_snapshots
          WHERE chart_snapshots.apple_id = shows.apple_id
          ORDER BY chart_snapshots.captured_at DESC
          LIMIT 1
        )
        WHERE ever_top_100 = 1
          AND chart_rank IS NULL
          AND EXISTS (
            SELECT 1
            FROM chart_snapshots
            WHERE chart_snapshots.apple_id = shows.apple_id
          )
        """
    )
    conn.commit()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def mark_chart_stale(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE shows SET in_top_100 = 0, apple_rank = NULL")


def mark_favorites_stale(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE shows SET favorite = 0, favorite_order = NULL")


def upsert_show(
    conn: sqlite3.Connection,
    *,
    apple_id: str,
    name: str,
    artist: str | None,
    apple_url: str | None,
    feed_url: str | None,
    artwork_url: str | None,
    genres: tuple[str, ...],
    chart_rank: int | None,
    favorite: bool,
    apple_rank: int | None = None,
    favorite_order: int | None = None,
) -> int:
    timestamp = now_iso()
    current_apple_rank = chart_rank if chart_rank is not None else apple_rank
    conn.execute(
        """
        INSERT INTO shows (
          apple_id, name, artist, apple_url, feed_url, artwork_url,
          genres_json, chart_rank, apple_rank, in_top_100, ever_top_100, favorite,
          favorite_order,
          first_seen_at, last_seen_at,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(apple_id) DO UPDATE SET
          name = excluded.name,
          artist = COALESCE(excluded.artist, shows.artist),
          apple_url = COALESCE(excluded.apple_url, shows.apple_url),
          feed_url = COALESCE(excluded.feed_url, shows.feed_url),
          artwork_url = COALESCE(excluded.artwork_url, shows.artwork_url),
          genres_json = CASE
            WHEN excluded.genres_json != '[]' THEN excluded.genres_json
            ELSE shows.genres_json
          END,
          chart_rank = CASE
            WHEN excluded.chart_rank IS NOT NULL THEN excluded.chart_rank
            ELSE shows.chart_rank
          END,
          apple_rank = excluded.apple_rank,
          in_top_100 = CASE
            WHEN excluded.chart_rank IS NOT NULL THEN 1
            ELSE shows.in_top_100
          END,
          ever_top_100 = MAX(shows.ever_top_100, excluded.ever_top_100),
          favorite = excluded.favorite,
          favorite_order = excluded.favorite_order,
          last_seen_at = excluded.last_seen_at,
          updated_at = excluded.updated_at
        """,
        (
            apple_id,
            name,
            artist,
            apple_url,
            feed_url,
            artwork_url,
            json.dumps(genres, ensure_ascii=False),
            chart_rank,
            current_apple_rank,
            int(chart_rank is not None),
            int(chart_rank is not None),
            int(favorite),
            favorite_order,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    row = conn.execute("SELECT id FROM shows WHERE apple_id = ?", (apple_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"show upsert failed: {apple_id}")
    return int(row["id"])


def update_show_feed_metadata(
    conn: sqlite3.Connection,
    show_id: int,
    *,
    description: str | None,
    homepage_url: str | None,
    artwork_url: str | None,
) -> None:
    conn.execute(
        """
        UPDATE shows
        SET description = COALESCE(?, description),
            homepage_url = COALESCE(?, homepage_url),
            artwork_url = COALESCE(artwork_url, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (description, homepage_url, artwork_url, now_iso(), show_id),
    )


def add_chart_snapshot(
    conn: sqlite3.Connection,
    *,
    captured_at: str,
    country: str,
    rank: int,
    apple_id: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO chart_snapshots (captured_at, country, rank, apple_id)
        VALUES (?, ?, ?, ?)
        """,
        (captured_at, country, rank, apple_id),
    )


def active_shows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM shows
            WHERE active = 1 AND (ever_top_100 = 1 OR favorite = 1)
            ORDER BY in_top_100 DESC, chart_rank IS NULL, chart_rank, name COLLATE NOCASE
            """
        )
    )


def show_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(in_top_100) AS top_100,
          SUM(favorite) AS favorites,
          SUM(CASE WHEN feed_url IS NULL OR feed_url = '' THEN 1 ELSE 0 END) AS missing_feeds
        FROM shows
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def upsert_episode(conn: sqlite3.Connection, show_id: int, episode: dict[str, Any]) -> tuple[int, bool]:
    timestamp = now_iso()
    existing = conn.execute(
        "SELECT id FROM episodes WHERE show_id = ? AND guid = ?",
        (show_id, episode["guid"]),
    ).fetchone()
    inserted = existing is None
    if inserted:
        conn.execute(
            """
            INSERT INTO episodes (
              show_id, guid, title, description, episode_url, audio_url,
              audio_type, image_url, published_at, duration, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                show_id,
                episode["guid"],
                episode["title"],
                episode.get("description"),
                episode.get("episode_url"),
                episode.get("audio_url"),
                episode.get("audio_type"),
                episode.get("image_url"),
                episode.get("published_at"),
                episode.get("duration"),
                timestamp,
                timestamp,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE episodes
            SET title = ?, description = ?, episode_url = ?, audio_url = ?,
                audio_type = ?, image_url = ?, published_at = ?, duration = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                episode["title"],
                episode.get("description"),
                episode.get("episode_url"),
                episode.get("audio_url"),
                episode.get("audio_type"),
                episode.get("image_url"),
                episode.get("published_at"),
                episode.get("duration"),
                timestamp,
                int(existing["id"]),
            ),
        )
    row = conn.execute(
        "SELECT id FROM episodes WHERE show_id = ? AND guid = ?",
        (show_id, episode["guid"]),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"episode upsert failed: {episode['guid']}")
    return int(row["id"]), inserted


def episodes_for_transcription(
    conn: sqlite3.Connection,
    *,
    limit: int | None,
    published_since: str | None,
    retry_failed: bool,
    episode_ids: Iterable[int] | None = None,
    force: bool = False,
    ranked_round_robin: bool = False,
) -> list[sqlite3.Row]:
    statuses = ("new", "transcription_failed") if retry_failed else ("new",)
    ids = tuple(episode_ids or ())
    if ranked_round_robin:
        if force or ids:
            raise ValueError("ranked round-robin transcription cannot use force or episode IDs")
        return _episodes_for_ranked_backfill(
            conn,
            statuses=statuses,
            published_since=published_since,
            per_show=limit if limit is not None else 2,
        )

    sql = """
        SELECT episodes.*, shows.name AS show_name, shows.apple_url AS show_apple_url
        FROM episodes
        JOIN shows ON shows.id = episodes.show_id
        WHERE episodes.audio_url IS NOT NULL
    """
    params: list[Any] = []
    if force:
        if not ids:
            raise ValueError("force transcription requires at least one episode ID")
    else:
        placeholders = ",".join("?" for _ in statuses)
        sql += f" AND episodes.status IN ({placeholders})"
        params.extend(statuses)
        sql += " AND shows.active = 1 AND (shows.in_top_100 = 1 OR shows.favorite = 1)"
    if ids:
        id_placeholders = ",".join("?" for _ in ids)
        sql += f" AND episodes.id IN ({id_placeholders})"
        params.extend(ids)
    if published_since:
        sql += " AND (episodes.published_at IS NULL OR episodes.published_at >= ?)"
        params.append(published_since)
    sql += " ORDER BY COALESCE(episodes.published_at, episodes.created_at) ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def _episodes_for_ranked_backfill(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...],
    published_since: str | None,
    per_show: int,
) -> list[sqlite3.Row]:
    if per_show <= 0:
        return []

    cursor = ranked_backfill_cursor(conn)
    status_placeholders = ",".join("?" for _ in statuses)
    date_sql = ""
    date_params: list[Any] = []
    if published_since:
        date_sql = " AND (candidate.published_at IS NULL OR candidate.published_at >= ?)"
        date_params.append(published_since)

    show = conn.execute(
        f"""
        SELECT shows.id, shows.chart_rank
        FROM shows
        WHERE shows.active = 1
          AND shows.in_top_100 = 1
          AND shows.chart_rank IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM episodes AS candidate
            WHERE candidate.show_id = shows.id
              AND candidate.audio_url IS NOT NULL
              AND candidate.status IN ({status_placeholders})
              {date_sql}
          )
        ORDER BY
          CASE WHEN shows.chart_rank >= ? THEN 0 ELSE 1 END,
          shows.chart_rank
        LIMIT 1
        """,
        [*statuses, *date_params, cursor],
    ).fetchone()
    if show is None:
        return []

    episode_date_sql = ""
    episode_date_params: list[Any] = []
    if published_since:
        episode_date_sql = (
            " AND (episodes.published_at IS NULL OR episodes.published_at >= ?)"
        )
        episode_date_params.append(published_since)

    return list(
        conn.execute(
            f"""
            SELECT
              episodes.*,
              shows.name AS show_name,
              shows.apple_url AS show_apple_url,
              shows.chart_rank AS queue_chart_rank,
              ? AS queue_cursor_rank
            FROM episodes
            JOIN shows ON shows.id = episodes.show_id
            WHERE episodes.show_id = ?
              AND episodes.audio_url IS NOT NULL
              AND episodes.status IN ({status_placeholders})
              {episode_date_sql}
            ORDER BY
              COALESCE(episodes.published_at, episodes.created_at) DESC,
              episodes.id DESC
            LIMIT ?
            """,
            [
                cursor,
                int(show["id"]),
                *statuses,
                *episode_date_params,
                per_show,
            ],
        )
    )


def ranked_backfill_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM pipeline_state WHERE key = 'ranked_backfill_next_chart_rank'"
    ).fetchone()
    if row is None:
        return 1
    try:
        return max(1, int(row["value"]))
    except (TypeError, ValueError):
        return 1


def advance_ranked_backfill_cursor(
    conn: sqlite3.Connection,
    *,
    completed_chart_rank: int,
) -> int:
    next_rank = completed_chart_rank + 1
    conn.execute(
        """
        INSERT INTO pipeline_state (key, value, updated_at)
        VALUES ('ranked_backfill_next_chart_rank', ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value = excluded.value,
          updated_at = excluded.updated_at
        """,
        (str(next_rank), now_iso()),
    )
    return next_rank


def set_transcript(
    conn: sqlite3.Connection,
    episode_id: int,
    *,
    transcript: str,
    path: pathlib.Path,
) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE episodes
        SET status = 'transcribed', transcript_text = ?, transcript_path = ?,
            transcribed_at = ?, error_message = NULL, updated_at = ?
        WHERE id = ?
        """,
        (transcript, str(path), timestamp, timestamp, episode_id),
    )


def mark_transcription_failed(conn: sqlite3.Connection, episode_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE episodes
        SET status = 'transcription_failed', error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (error[-4000:], now_iso(), episode_id),
    )


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM episodes GROUP BY status ORDER BY status"
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def list_episodes(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ""
    values = tuple(statuses or ())
    if values:
        placeholders = ",".join("?" for _ in values)
        where = f"WHERE episodes.status IN ({placeholders})"
        params.extend(values)
    params.append(limit)
    return list(
        conn.execute(
            f"""
            SELECT episodes.*, shows.name AS show_name
            FROM episodes
            JOIN shows ON shows.id = episodes.show_id
            {where}
            ORDER BY COALESCE(episodes.published_at, episodes.created_at) DESC
            LIMIT ?
            """,
            params,
        )
    )
