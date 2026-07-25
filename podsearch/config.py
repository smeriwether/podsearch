from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    tomllib = None


@dataclasses.dataclass(frozen=True)
class AppConfig:
    timezone: str = "America/New_York"
    database_path: pathlib.Path = pathlib.Path("var/podsearch.sqlite3")
    public_dir: pathlib.Path = pathlib.Path("public")
    state_dir: pathlib.Path = pathlib.Path("var")
    user_agent: str = "podsearch/0.1"
    processed_after: str = "2026-01-01"
    nightly_lookback_days: int = 3
    nightly_transcription_limit: int = 100


@dataclasses.dataclass(frozen=True)
class ChartConfig:
    country: str = "us"
    limit: int = 100
    url: str = "https://rss.marketingtools.apple.com/api/v2/{country}/podcasts/top/{limit}/podcasts.json"
    extended_url: str = "https://itunes.apple.com/{country}/rss/toppodcasts/limit=200/json"

    @property
    def resolved_url(self) -> str:
        return self.url.format(country=self.country.lower(), limit=self.limit)

    @property
    def resolved_extended_url(self) -> str:
        return self.extended_url.format(country=self.country.lower())


@dataclasses.dataclass(frozen=True)
class TranscriptionConfig:
    provider: str = "command"
    command: str = "whisper-cli"
    args: tuple[str, ...] = (
        "-m",
        "models/ggml-small.en.bin",
        "-f",
        "{audio_path}",
        "-otxt",
        "-of",
        "{output_stem}",
    )
    output_path: str = "{output_stem}.txt"
    audio_dir: pathlib.Path = pathlib.Path("var/audio")
    transcript_dir: pathlib.Path = pathlib.Path("var/transcripts")
    max_audio_mb: int = 750
    keep_audio: bool = False


@dataclasses.dataclass(frozen=True)
class SiteConfig:
    title: str = "Podsearch"
    description: str = "Search full podcast transcripts, right in your browser."
    base_url: str = "https://podsearch.merimerimeri.com"
    host: str = "127.0.0.1"
    port: int = 8091


@dataclasses.dataclass(frozen=True)
class Config:
    root: pathlib.Path
    app: AppConfig
    chart: ChartConfig
    transcription: TranscriptionConfig
    site: SiteConfig
    favorites: tuple[str, ...]
    feed_overrides: dict[str, str]


def load_config(path: str | pathlib.Path) -> Config:
    config_path = pathlib.Path(path).expanduser().resolve()
    raw = _load_toml(config_path)
    root = config_path.parent
    app = _dataclass_from_dict(AppConfig, raw.get("app", {}))
    transcription = _dataclass_from_dict(TranscriptionConfig, raw.get("transcription", {}))
    return Config(
        root=root,
        app=dataclasses.replace(
            app,
            database_path=_resolve(root, app.database_path),
            public_dir=_resolve(root, app.public_dir),
            state_dir=_resolve(root, app.state_dir),
        ),
        chart=_dataclass_from_dict(ChartConfig, raw.get("chart", {})),
        transcription=dataclasses.replace(
            transcription,
            args=tuple(transcription.args),
            audio_dir=_resolve(root, transcription.audio_dir),
            transcript_dir=_resolve(root, transcription.transcript_dir),
        ),
        site=_dataclass_from_dict(SiteConfig, raw.get("site", {})),
        favorites=tuple(str(value) for value in raw.get("favorites", [])),
        feed_overrides={
            str(key): str(value)
            for key, value in raw.get("feed_overrides", {}).items()
        },
    )


def _dataclass_from_dict(cls: type[Any], raw: dict[str, Any]) -> Any:
    fields = {field.name for field in dataclasses.fields(cls)}
    return cls(**{key: value for key, value in raw.items() if key in fields})


def _resolve(root: pathlib.Path, value: pathlib.Path | str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _load_toml(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)
    return _parse_basic_toml(text)


def _parse_basic_toml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current: dict[str, Any] = root
    lines = iter(enumerate(text.splitlines(), start=1))
    for line_number, line in lines:
        stripped = _strip_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = root.setdefault(stripped[1:-1].strip(), {})
            continue
        if "=" not in stripped:
            raise ValueError(f"invalid TOML line {line_number}: {line}")
        key, raw_value = (part.strip() for part in stripped.split("=", 1))
        if raw_value.startswith("[") and not raw_value.endswith("]"):
            parts = [raw_value]
            for _, continuation in lines:
                value = _strip_comment(continuation).strip()
                if value:
                    parts.append(value)
                if value.endswith("]"):
                    break
            raw_value = " ".join(parts)
        current[key] = _parse_value(raw_value)
    return root


def _strip_comment(line: str) -> str:
    in_string = False
    escaped = False
    output: list[str] = []
    for char in line:
        if char == "\\" and in_string:
            escaped = not escaped
            output.append(char)
            continue
        if char == '"' and not escaped:
            in_string = not in_string
        escaped = False
        if char == "#" and not in_string:
            break
        output.append(char)
    return "".join(output)


def _parse_value(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("[") and value.endswith("]"):
        return ast.literal_eval(value)
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value
