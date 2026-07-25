from __future__ import annotations

import hashlib
import html
import re


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def stable_guid(title: str, published: str | None) -> str:
    return hashlib.sha256(f"{title}|{published or ''}".encode("utf-8")).hexdigest()


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "episode"
