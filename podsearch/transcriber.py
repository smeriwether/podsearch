from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import urllib.parse
import urllib.request

from . import storage
from .config import Config
from .text import clean_text, slugify, strip_html


class TranscriptionError(RuntimeError):
    pass


MAX_PROMPT_DESCRIPTION_CHARS = 240


def process_queue(
    config: Config,
    conn,
    *,
    limit: int | None = None,
    published_since: str | None = None,
    retry_failed: bool = False,
    command_output: bool = True,
    episode_ids: tuple[int, ...] = (),
    force: bool = False,
    ranked_round_robin: bool = False,
) -> dict[str, int]:
    stats = {"queued": 0, "transcribed": 0, "failed": 0}
    episodes = storage.episodes_for_transcription(
        conn,
        limit=limit,
        published_since=published_since,
        retry_failed=retry_failed,
        episode_ids=episode_ids,
        force=force,
        ranked_round_robin=ranked_round_robin,
    )
    stats["queued"] = len(episodes)
    if ranked_round_robin and episodes:
        stats["chart_rank"] = int(episodes[0]["queue_chart_rank"])
        stats["queue_wrapped"] = int(
            episodes[0]["queue_chart_rank"] < episodes[0]["queue_cursor_rank"]
        )
    for episode in episodes:
        try:
            transcribe_episode(
                config,
                conn,
                episode,
                command_output=command_output,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the rest of the nightly queue
            if not force:
                storage.mark_transcription_failed(conn, int(episode["id"]), str(exc))
                conn.commit()
            stats["failed"] += 1
        else:
            stats["transcribed"] += 1
    if ranked_round_robin and episodes:
        stats["next_chart_rank"] = storage.advance_ranked_backfill_cursor(
            conn,
            completed_chart_rank=int(episodes[0]["queue_chart_rank"]),
        )
        conn.commit()
    return stats


def transcribe_episode(
    config: Config,
    conn,
    episode,
    *,
    command_output: bool,
    force: bool = False,
) -> pathlib.Path:
    if config.transcription.provider != "command":
        raise TranscriptionError(f"unsupported transcription provider: {config.transcription.provider}")
    audio_url = str(episode["audio_url"] or "")
    if not audio_url:
        raise TranscriptionError("episode has no audio enclosure")
    executable = shutil.which(config.transcription.command)
    if executable is None:
        raise TranscriptionError(f"transcription command not found: {config.transcription.command}")

    config.transcription.audio_dir.mkdir(parents=True, exist_ok=True)
    config.transcription.transcript_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{episode['id']}-{slugify(str(episode['title']))[:80]}"
    audio_path = config.transcription.audio_dir / f"{stem}{_audio_suffix(audio_url)}"
    output_stem = config.transcription.transcript_dir / stem
    run_output_stem = (
        config.transcription.transcript_dir / f".{stem}.retranscribe"
        if force
        else output_stem
    )
    description = strip_html(str(episode["description"] or ""))
    if len(description) > MAX_PROMPT_DESCRIPTION_CHARS:
        description = description[:MAX_PROMPT_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
    context = {
        "audio_path": str(audio_path),
        "output_stem": str(run_output_stem),
        "output_dir": str(config.transcription.transcript_dir),
        "episode_id": str(episode["id"]),
        "show_name": str(episode["show_name"] or ""),
        "episode_title": str(episode["title"] or ""),
        "episode_description": description,
    }
    run_output_path = pathlib.Path(config.transcription.output_path.format(**context))
    canonical_context = {**context, "output_stem": str(output_stem)}
    output_path = pathlib.Path(config.transcription.output_path.format(**canonical_context))

    if force or not output_path.exists():
        run_output_path.unlink(missing_ok=True)
        if not audio_path.exists():
            _download_audio(config, audio_url, audio_path)
        args = [arg.format(**context) for arg in config.transcription.args]
        _run([executable, *args], command_output=command_output)

    transcript = clean_text(run_output_path.read_text(encoding="utf-8"))
    if not transcript:
        raise TranscriptionError(f"transcription command produced an empty file: {run_output_path}")
    if run_output_path != output_path:
        run_output_path.replace(output_path)
    storage.set_transcript(conn, int(episode["id"]), transcript=transcript, path=output_path)
    conn.commit()
    if not config.transcription.keep_audio and audio_path.exists():
        audio_path.unlink()
    return output_path


def _download_audio(config: Config, url: str, output_path: pathlib.Path) -> None:
    max_bytes = config.transcription.max_audio_mb * 1024 * 1024
    request = urllib.request.Request(url, headers={"User-Agent": config.app.user_agent})
    partial_path = output_path.with_name(f".{output_path.name}.part")
    partial_path.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise TranscriptionError(f"audio is larger than max_audio_mb: {length} bytes")
            written = 0
            with partial_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise TranscriptionError("audio exceeded max_audio_mb while downloading")
                    output.write(chunk)
        os.replace(partial_path, output_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def _run(command: list[str], *, command_output: bool) -> None:
    if command_output:
        subprocess.run(command, check=True)
        return
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()[-2000:]
    suffix = f": {detail}" if detail else ""
    raise TranscriptionError(f"transcription command failed with exit {result.returncode}{suffix}")


def _audio_suffix(url: str) -> str:
    suffix = pathlib.Path(urllib.parse.urlparse(url).path).suffix
    return suffix if suffix and len(suffix) <= 8 else ".mp3"
