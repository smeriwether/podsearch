from __future__ import annotations

import datetime as dt
import email.utils
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from . import storage
from .config import Config
from .text import clean_text, stable_guid, strip_html


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    description: str | None
    homepage_url: str | None
    image_url: str | None
    episodes: list[dict[str, Any]]


def fetch_feed(url: str, user_agent: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def ingest(
    config: Config,
    conn,
    *,
    published_since: str | None = None,
    limit_per_feed: int | None = None,
) -> dict[str, int]:
    since = _parse_cutoff(published_since or config.app.processed_after)
    stats = {
        "feeds": 0,
        "feed_errors": 0,
        "episodes_seen": 0,
        "episodes_inserted": 0,
        "episodes_before_since": 0,
    }
    for show in storage.active_shows(conn):
        feed_url = str(show["feed_url"] or "")
        if not feed_url:
            stats["feed_errors"] += 1
            print(f"warning: no feed URL: {show['name']}", file=sys.stderr)
            continue
        try:
            parsed = parse_feed(fetch_feed(feed_url, config.app.user_agent), str(show["name"]))
        except Exception as exc:  # noqa: BLE001 - continue through a large catalog
            stats["feed_errors"] += 1
            print(f"warning: feed failed: {show['name']}: {exc}", file=sys.stderr)
            continue
        stats["feeds"] += 1
        storage.update_show_feed_metadata(
            conn,
            int(show["id"]),
            description=parsed.description,
            homepage_url=parsed.homepage_url,
            artwork_url=parsed.image_url,
        )
        kept = 0
        for episode in parsed.episodes:
            if since is not None and not _episode_is_since(episode, since):
                stats["episodes_before_since"] += 1
                continue
            _, inserted = storage.upsert_episode(conn, int(show["id"]), episode)
            stats["episodes_seen"] += 1
            stats["episodes_inserted"] += int(inserted)
            kept += 1
            if limit_per_feed is not None and kept >= limit_per_feed:
                break
        conn.commit()
    return stats


def parse_feed(xml_bytes: bytes, fallback_name: str) -> ParsedFeed:
    root = ET.fromstring(xml_bytes)
    if _local(root.tag) == "feed":
        return _parse_atom(root, fallback_name)
    channel = _first(root, "channel")
    if channel is None:
        channel = root
    title = _text(channel, "title") or fallback_name
    description = strip_html(_text(channel, "description") or _text(channel, "subtitle"))
    homepage_url = _text(channel, "link")
    image_url = _feed_image(channel)
    episodes = [
        episode
        for item in _children(channel, "item")
        if (episode := _parse_rss_item(item, image_url)) is not None
    ]
    return ParsedFeed(title, description, homepage_url, image_url, episodes)


def _parse_rss_item(item: ET.Element, feed_image_url: str | None) -> dict[str, Any] | None:
    title = clean_text(_text(item, "title"))
    if not title:
        return None
    published_at = _parse_date(_text(item, "pubDate") or _text(item, "published"))
    episode_url = _text(item, "link")
    guid = _text(item, "guid") or episode_url or stable_guid(title, published_at)
    audio_url, audio_type = _enclosure(item)
    description = strip_html(
        _text(item, "description") or _text(item, "summary") or _text(item, "encoded")
    )
    return {
        "guid": guid,
        "title": title,
        "description": description,
        "episode_url": episode_url,
        "audio_url": audio_url,
        "audio_type": audio_type,
        "image_url": _item_image(item) or feed_image_url,
        "published_at": published_at,
        "duration": _text(item, "duration"),
    }


def _parse_atom(root: ET.Element, fallback_name: str) -> ParsedFeed:
    title = _text(root, "title") or fallback_name
    description = strip_html(_text(root, "subtitle") or _text(root, "summary"))
    homepage_url = _atom_link(root, "alternate") or _atom_link(root, None)
    image_url = _text(root, "logo") or _text(root, "icon")
    episodes: list[dict[str, Any]] = []
    for entry in _children(root, "entry"):
        episode_title = clean_text(_text(entry, "title"))
        if not episode_title:
            continue
        published_at = _parse_date(_text(entry, "published") or _text(entry, "updated"))
        episode_url = _atom_link(entry, "alternate") or _atom_link(entry, None)
        audio_url = _atom_link(entry, "enclosure")
        episodes.append(
            {
                "guid": _text(entry, "id") or episode_url or stable_guid(episode_title, published_at),
                "title": episode_title,
                "description": strip_html(_text(entry, "summary") or _text(entry, "content")),
                "episode_url": episode_url,
                "audio_url": audio_url,
                "audio_type": None,
                "image_url": _item_image(entry) or image_url,
                "published_at": published_at,
                "duration": _text(entry, "duration"),
            }
        )
    return ParsedFeed(title, description, homepage_url, image_url, episodes)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _first(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in list(element) if _local(child.tag) == name), None)


def _text(element: ET.Element, name: str) -> str | None:
    child = _first(element, name)
    if child is not None and child.text:
        return clean_text(child.text)
    for descendant in element.iter():
        if descendant is not element and _local(descendant.tag) == name and descendant.text:
            return clean_text(descendant.text)
    return None


def _feed_image(channel: ET.Element) -> str | None:
    image = _first(channel, "image")
    return (_text(image, "url") if image is not None else None) or _item_image(channel)


def _item_image(element: ET.Element) -> str | None:
    for descendant in element.iter():
        if _local(descendant.tag) in {"image", "thumbnail", "content"}:
            value = descendant.attrib.get("href") or descendant.attrib.get("url")
            if value and any(token in value.lower() for token in ("image", ".jpg", ".png", ".webp")):
                return value
    return None


def _enclosure(item: ET.Element) -> tuple[str | None, str | None]:
    for descendant in item.iter():
        if _local(descendant.tag) != "enclosure":
            continue
        url = descendant.attrib.get("url")
        media_type = descendant.attrib.get("type")
        if url and (not media_type or media_type.startswith("audio/")):
            return url, media_type
    for descendant in item.iter():
        if _local(descendant.tag) not in {"content", "link"}:
            continue
        url = descendant.attrib.get("url") or descendant.attrib.get("href")
        media_type = descendant.attrib.get("type")
        if url and (media_type or "").startswith("audio/"):
            return url, media_type
    return None, None


def _atom_link(element: ET.Element, rel: str | None) -> str | None:
    for child in _children(element, "link"):
        if rel is None or child.attrib.get("rel") == rel:
            return child.attrib.get("href")
    return None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _parse_cutoff(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.fromisoformat(f"{value}T00:00:00+00:00")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _episode_is_since(episode: dict[str, Any], since: dt.datetime) -> bool:
    published_at = episode.get("published_at")
    if not published_at:
        return True
    try:
        parsed = dt.datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc) >= since
