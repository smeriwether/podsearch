from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import storage
from .config import Config


LOOKUP_URL = "https://itunes.apple.com/lookup"
APPLE_ID_PATTERN = re.compile(r"(?:^|/id)(\d+)(?:$|[?/#])")


@dataclass(frozen=True)
class CatalogShow:
    apple_id: str
    name: str
    artist: str | None
    apple_url: str | None
    feed_url: str | None
    artwork_url: str | None
    genres: tuple[str, ...]
    rank: int | None
    apple_rank: int | None
    favorite: bool
    favorite_order: int | None


def fetch_json(url: str, user_agent: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def favorite_apple_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    ids: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped.isdigit():
            ids.append(stripped)
            continue
        match = APPLE_ID_PATTERN.search(stripped)
        if not match:
            raise ValueError(f"favorite is not an Apple Podcasts URL or ID: {value}")
        ids.append(match.group(1))
    return tuple(dict.fromkeys(ids))


def fetch_catalog(config: Config) -> tuple[list[CatalogShow], str]:
    chart = fetch_json(config.chart.resolved_url, config.app.user_agent)
    extended_chart = fetch_json(
        config.chart.resolved_extended_url,
        config.app.user_agent,
    )
    extended_rankings = extended_chart_rankings(extended_chart)
    feed = chart.get("feed") or {}
    results = feed.get("results") or []
    captured_at = str(feed.get("updated") or storage.now_iso())
    ranked: list[dict[str, Any]] = []
    for rank, item in enumerate(results, start=1):
        if not item.get("id"):
            continue
        ranked.append({**item, "rank": rank})

    favorites = favorite_apple_ids(config.favorites)
    favorite_order = {
        apple_id: position for position, apple_id in enumerate(favorites, start=1)
    }
    lookup_ids = list(dict.fromkeys([str(item["id"]) for item in ranked] + list(favorites)))
    details = lookup(lookup_ids, config.app.user_agent)
    by_id = {str(item.get("collectionId")): item for item in details if item.get("collectionId")}

    shows: list[CatalogShow] = []
    ranked_ids = {str(item["id"]) for item in ranked}
    for item in ranked:
        apple_id = str(item["id"])
        detail = by_id.get(apple_id, {})
        shows.append(
            CatalogShow(
                apple_id=apple_id,
                name=str(detail.get("collectionName") or item.get("name") or apple_id),
                artist=_optional(detail.get("artistName") or item.get("artistName")),
                apple_url=_optional(detail.get("collectionViewUrl") or item.get("url")),
                feed_url=_optional(
                    config.feed_overrides.get(apple_id) or detail.get("feedUrl")
                ),
                artwork_url=_optional(
                    detail.get("artworkUrl600")
                    or detail.get("artworkUrl100")
                    or item.get("artworkUrl100")
                ),
                genres=_genres(detail, item),
                rank=int(item["rank"]),
                apple_rank=extended_rankings.get(apple_id, int(item["rank"])),
                favorite=apple_id in favorites,
                favorite_order=favorite_order.get(apple_id),
            )
        )
    for apple_id in favorites:
        if apple_id in ranked_ids:
            continue
        detail = by_id.get(apple_id)
        if not detail:
            raise RuntimeError(f"Apple lookup returned no podcast for favorite ID {apple_id}")
        shows.append(
            CatalogShow(
                apple_id=apple_id,
                name=str(detail.get("collectionName") or apple_id),
                artist=_optional(detail.get("artistName")),
                apple_url=_optional(detail.get("collectionViewUrl")),
                feed_url=_optional(
                    config.feed_overrides.get(apple_id) or detail.get("feedUrl")
                ),
                artwork_url=_optional(detail.get("artworkUrl600") or detail.get("artworkUrl100")),
                genres=_genres(detail),
                rank=None,
                apple_rank=extended_rankings.get(apple_id),
                favorite=True,
                favorite_order=favorite_order[apple_id],
            )
        )
    return shows, captured_at


def extended_chart_rankings(payload: dict[str, Any]) -> dict[str, int]:
    entries = (payload.get("feed") or {}).get("entry") or []
    rankings: dict[str, int] = {}
    for rank, entry in enumerate(entries, start=1):
        apple_id = (
            ((entry.get("id") or {}).get("attributes") or {}).get("im:id")
            if isinstance(entry, dict)
            else None
        )
        if apple_id:
            rankings[str(apple_id)] = rank
    return rankings


def lookup(apple_ids: list[str], user_agent: str) -> list[dict[str, Any]]:
    if not apple_ids:
        return []
    results: list[dict[str, Any]] = []
    for start in range(0, len(apple_ids), 50):
        query = urllib.parse.urlencode(
            {
                "id": ",".join(apple_ids[start : start + 50]),
                "entity": "podcast",
                "country": "us",
            }
        )
        payload = fetch_json(f"{LOOKUP_URL}?{query}", user_agent)
        results.extend(payload.get("results") or [])
    return results


def sync_catalog(config: Config, conn) -> dict[str, int]:
    shows, captured_at = fetch_catalog(config)
    storage.mark_chart_stale(conn)
    storage.mark_favorites_stale(conn)
    missing_feeds = 0
    for show in shows:
        storage.upsert_show(
            conn,
            apple_id=show.apple_id,
            name=show.name,
            artist=show.artist,
            apple_url=show.apple_url,
            feed_url=show.feed_url,
            artwork_url=show.artwork_url,
            genres=show.genres,
            chart_rank=show.rank,
            favorite=show.favorite,
            apple_rank=show.apple_rank,
            favorite_order=show.favorite_order,
        )
        if show.rank is not None:
            storage.add_chart_snapshot(
                conn,
                captured_at=captured_at,
                country=config.chart.country,
                rank=show.rank,
                apple_id=show.apple_id,
            )
        if not show.feed_url:
            missing_feeds += 1
    conn.commit()
    return {
        "chart_shows": sum(1 for show in shows if show.rank is not None),
        "favorite_shows": sum(1 for show in shows if show.favorite),
        "ranked_favorites": sum(
            1 for show in shows if show.favorite and show.apple_rank is not None
        ),
        "missing_feeds": missing_feeds,
    }


def _optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _genres(*items: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for item in items:
        values = item.get("genres") or []
        for value in values:
            name = value.get("name") if isinstance(value, dict) else value
            text = str(name or "").strip()
            if text and text.lower() != "podcasts" and text not in names:
                names.append(text)
    return tuple(names)
