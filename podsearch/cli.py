from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import shutil
import sqlite3
import sys

from . import apple, distributed, feeds, launchd, site, storage, transcriber
from .config import Config, load_config
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podsearch")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="Check the local runtime and data store")
    commands.add_parser("sync-catalog", help="Refresh Apple Top 100 and favorites metadata")

    ingest = commands.add_parser("ingest", help="Fetch episode metadata from active RSS feeds")
    ingest.add_argument("--since", default=None)
    ingest.add_argument("--limit-per-feed", type=int, default=None)

    transcribe = commands.add_parser("transcribe", help="Transcribe queued episodes locally")
    transcribe.add_argument("--since", default=None)
    transcribe.add_argument("--limit", type=int, default=None)
    transcribe.add_argument("--retry-failed", action="store_true")
    transcribe.add_argument("--quiet-command", action="store_true")
    transcribe.add_argument(
        "--ranked-round-robin",
        action="store_true",
        help=(
            "Process the newest missing episodes for one Top 100 podcast, "
            "then advance a persistent chart-rank cursor."
        ),
    )
    transcribe.add_argument(
        "--force",
        action="store_true",
        help="Re-transcribe selected --episode-id values even when already complete.",
    )
    transcribe.add_argument(
        "--episode-id",
        type=int,
        action="append",
        default=[],
        help="Only transcribe this episode ID. Repeatable.",
    )

    commands.add_parser("build-site", help="Build static assets and the public SQLite index")
    nightly = commands.add_parser(
        "run-nightly", help="Refresh, transcribe, and rebuild the static site"
    )
    nightly.add_argument(
        "--metadata-only",
        action="store_true",
        help="Discover and save new episodes, then rebuild without transcribing.",
    )

    list_parser = commands.add_parser("list", help="Show counts or recent episode records")
    list_parser.add_argument("--status", action="append", default=[])
    list_parser.add_argument("--limit", type=int, default=20)

    serve_parser = commands.add_parser("serve", help="Serve the static site on localhost")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    launchd_parser = commands.add_parser("launchd-install", help="Write nightly and server LaunchAgents")
    launchd_parser.add_argument("--hour", type=int, default=2)
    launchd_parser.add_argument("--minute", type=int, default=30)

    snapshot_parser = commands.add_parser(
        "export-worker-snapshot",
        help="Claim oldest episodes and export a lean remote-worker database",
    )
    snapshot_parser.add_argument("--output", required=True)
    snapshot_parser.add_argument("--worker-id", required=True)
    snapshot_parser.add_argument("--claim-limit", type=int, default=200)
    snapshot_parser.add_argument("--lease-hours", type=int, default=72)
    snapshot_parser.add_argument("--since", default="2026-01-01")

    worker_parser = commands.add_parser(
        "worker-transcribe",
        help="Transcribe leased remote-worker episodes and write result bundles",
    )
    worker_parser.add_argument("--worker-id", required=True)
    worker_parser.add_argument("--outbox", default="var/worker-outbox")
    worker_parser.add_argument("--limit", type=int, default=1)
    worker_parser.add_argument("--retry-failed", action="store_true")
    worker_parser.add_argument("--quiet-command", action="store_true")

    import_parser = commands.add_parser(
        "import-transcript-results",
        help="Import idempotent transcript result bundles into the primary database",
    )
    import_parser.add_argument("inbox", nargs="?", default="var/worker-inbox")

    remote_pull_parser = commands.add_parser(
        "remote-pull-install",
        help="Install the Mac mini LaunchAgent that pulls remote transcript bundles",
    )
    remote_pull_parser.add_argument("--worker", required=True, help="SSH host, e.g. user@macbook")
    remote_pull_parser.add_argument("--remote-repo", required=True)
    remote_pull_parser.add_argument("--interval", type=int, default=300)

    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "doctor":
        return doctor(config)
    if args.command == "serve":
        return serve(
            config.app.public_dir,
            args.host or config.site.host,
            args.port or config.site.port,
        )
    if args.command == "launchd-install":
        nightly, server, tunnel, backfill = launchd.install(
            config, hour=args.hour, minute=args.minute
        )
        print(f"nightly_plist={nightly}")
        print(f"server_plist={server}")
        print(f"tunnel_plist={tunnel}")
        print(f"backfill_plist={backfill}")
        return 0
    if args.command == "remote-pull-install":
        path = launchd.install_remote_pull(
            config,
            worker=args.worker,
            remote_repo=args.remote_repo,
            interval_seconds=args.interval,
        )
        print(f"remote_pull_plist={path}")
        return 0

    with storage.connect(config) as conn:
        if args.command == "sync-catalog":
            _print_stats(apple.sync_catalog(config, conn))
        elif args.command == "ingest":
            _print_stats(
                feeds.ingest(
                    config,
                    conn,
                    published_since=args.since,
                    limit_per_feed=args.limit_per_feed,
                )
            )
        elif args.command == "transcribe":
            _print_stats(
                transcriber.process_queue(
                    config,
                    conn,
                    limit=args.limit,
                    published_since=args.since,
                    retry_failed=args.retry_failed,
                    command_output=not args.quiet_command,
                    episode_ids=tuple(args.episode_id),
                    force=args.force,
                    ranked_round_robin=args.ranked_round_robin,
                )
            )
        elif args.command == "build-site":
            _print_stats(site.build_site(config, conn))
        elif args.command == "export-worker-snapshot":
            _print_stats(
                distributed.export_worker_snapshot(
                    conn,
                    pathlib.Path(args.output),
                    worker_id=args.worker_id,
                    claim_limit=args.claim_limit,
                    lease_hours=args.lease_hours,
                    published_since=args.since,
                )
            )
        elif args.command == "worker-transcribe":
            _print_stats(
                distributed.process_worker_batch(
                    config,
                    conn,
                    worker_id=args.worker_id,
                    outbox=pathlib.Path(args.outbox),
                    limit=args.limit,
                    retry_failed=args.retry_failed,
                    command_output=not args.quiet_command,
                )
            )
        elif args.command == "import-transcript-results":
            _print_stats(
                distributed.import_result_bundles(
                    config,
                    conn,
                    pathlib.Path(args.inbox),
                )
            )
        elif args.command == "run-nightly":
            _print_stats(run_nightly(config, conn, metadata_only=args.metadata_only))
        elif args.command == "list":
            if args.status:
                for episode in storage.list_episodes(
                    conn, statuses=args.status, limit=args.limit
                ):
                    print(
                        f"{episode['id']}\t{episode['status']}\t"
                        f"{episode['published_at'] or ''}\t{episode['show_name']}\t"
                        f"{episode['title']}"
                    )
            else:
                _print_stats({f"shows_{k}": v for k, v in storage.show_counts(conn).items()})
                _print_stats({f"episodes_{k}": v for k, v in storage.status_counts(conn).items()})
        else:
            parser.error(f"unsupported command: {args.command}")
    return 0


def run_nightly(
    config: Config,
    conn: sqlite3.Connection,
    *,
    metadata_only: bool = False,
) -> dict[str, int]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=config.app.nightly_lookback_days
    )
    since = cutoff.replace(microsecond=0).isoformat()
    stats: dict[str, int] = {}
    _merge(stats, "catalog", apple.sync_catalog(config, conn))
    _merge(stats, "ingest", feeds.ingest(config, conn, published_since=since))
    if not metadata_only:
        _merge(
            stats,
            "transcription",
            transcriber.process_queue(
                config,
                conn,
                limit=config.app.nightly_transcription_limit,
                retry_failed=True,
                command_output=False,
            ),
        )
    _merge(stats, "site", site.build_site(config, conn))
    return stats


def doctor(config: Config) -> int:
    errors: list[str] = []
    warnings: list[str] = []

    if config.chart.limit != 100:
        warnings.append(f"chart.limit is {config.chart.limit}; the requested source list is 100")
    if shutil.which(config.transcription.command) is None:
        errors.append(f"transcription command not found: {config.transcription.command}")
    model = _model_path(config)
    if model and not model.exists():
        errors.append(f"transcription model not found: {model}")
    sqlite_dist = config.root / "node_modules" / "@sqlite.org" / "sqlite-wasm" / "dist"
    if not (sqlite_dist / "index.mjs").exists() or not (sqlite_dist / "sqlite3.wasm").exists():
        errors.append("SQLite Wasm assets missing; run npm install")

    try:
        with storage.connect(config) as conn:
            conn.execute("CREATE VIRTUAL TABLE temp.fts_check USING fts5(value)")
            counts = storage.status_counts(conn)
            shows = storage.show_counts(conn)
    except sqlite3.Error as exc:
        errors.append(f"SQLite/FTS5 unavailable: {exc}")
        counts = {}
        shows = {}

    config.app.state_dir.mkdir(parents=True, exist_ok=True)
    print(f"config_root={config.root}")
    print(f"database={config.app.database_path}")
    print(f"public_dir={config.app.public_dir}")
    print(f"chart={config.chart.resolved_url}")
    print(f"transcription_command={shutil.which(config.transcription.command) or 'missing'}")
    print(f"transcription_model={model or 'not configured'}")
    print(f"show_counts={shows}")
    print(f"episode_status_counts={counts}")
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


def _model_path(config: Config) -> pathlib.Path | None:
    args = config.transcription.args
    for index, arg in enumerate(args):
        if arg in {"-m", "--model"} and index + 1 < len(args):
            path = pathlib.Path(args[index + 1]).expanduser()
            return path if path.is_absolute() else config.root / path
    return None


def _merge(target: dict[str, int], prefix: str, values: dict[str, int]) -> None:
    target.update({f"{prefix}_{key}": value for key, value in values.items()})


def _print_stats(stats: dict[str, int]) -> None:
    for key, value in stats.items():
        print(f"{key}={value}")
