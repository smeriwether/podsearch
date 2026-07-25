from __future__ import annotations

import pathlib
import plistlib

from .config import Config


NIGHTLY_LABEL = "com.merimeri.podsearch.nightly"
SERVER_LABEL = "com.merimeri.podsearch.server"
TUNNEL_LABEL = "com.merimeri.podsearch.tunnel"
BACKFILL_LABEL = "com.merimeri.podsearch.backfill"
REMOTE_PULL_LABEL = "com.merimeri.podsearch.remote-pull"


def install(
    config: Config, *, hour: int, minute: int
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    home = pathlib.Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    log_dir = config.app.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = ":".join(
        (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
            str(home / ".local" / "share" / "mise" / "shims"),
            str(home / ".local" / "bin"),
        )
    )

    common = {
        "WorkingDirectory": str(config.root),
        "EnvironmentVariables": {
            "PATH": runtime_path,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    }
    nightly = {
        "Label": NIGHTLY_LABEL,
        "ProgramArguments": ["/bin/zsh", str(config.root / "scripts" / "nightly.sh")],
        **common,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_dir / "nightly.out.log"),
        "StandardErrorPath": str(log_dir / "nightly.err.log"),
    }
    server = {
        "Label": SERVER_LABEL,
        "ProgramArguments": [
            "/usr/bin/python3",
            "-m",
            "podsearch",
            "--config",
            str(config.root / "config.toml"),
            "serve",
        ],
        **common,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "server.out.log"),
        "StandardErrorPath": str(log_dir / "server.err.log"),
    }
    tunnel = {
        "Label": TUNNEL_LABEL,
        "ProgramArguments": [
            "/opt/homebrew/bin/cloudflared",
            "tunnel",
            "--config",
            str(home / ".cloudflared" / "podsearch.yml"),
            "--no-autoupdate",
            "run",
            "podsearch",
        ],
        **common,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "tunnel.out.log"),
        "StandardErrorPath": str(log_dir / "tunnel.err.log"),
    }
    backfill = {
        "Label": BACKFILL_LABEL,
        "ProgramArguments": [
            "/bin/zsh",
            str(config.root / "scripts" / "backfill-2026.sh"),
        ],
        **common,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "backfill.out.log"),
        "StandardErrorPath": str(log_dir / "backfill.err.log"),
    }

    nightly_path = launch_agents / f"{NIGHTLY_LABEL}.plist"
    server_path = launch_agents / f"{SERVER_LABEL}.plist"
    tunnel_path = launch_agents / f"{TUNNEL_LABEL}.plist"
    backfill_path = launch_agents / f"{BACKFILL_LABEL}.plist"
    for path, payload in (
        (nightly_path, nightly),
        (server_path, server),
        (tunnel_path, tunnel),
        (backfill_path, backfill),
    ):
        with path.open("wb") as output:
            plistlib.dump(payload, output, sort_keys=False)
    return nightly_path, server_path, tunnel_path, backfill_path


def install_remote_pull(
    config: Config,
    *,
    worker: str,
    remote_repo: str,
    interval_seconds: int,
) -> pathlib.Path:
    if not worker.strip():
        raise ValueError("worker SSH host is required")
    if not remote_repo.startswith("/"):
        raise ValueError("remote repository path must be absolute")
    if interval_seconds < 60:
        raise ValueError("remote pull interval must be at least 60 seconds")

    home = pathlib.Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    log_dir = config.app.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = ":".join(
        (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
            str(home / ".local" / "share" / "mise" / "shims"),
            str(home / ".local" / "bin"),
        )
    )
    payload = {
        "Label": REMOTE_PULL_LABEL,
        "ProgramArguments": [
            "/bin/zsh",
            str(config.root / "scripts" / "pull-remote-transcripts.sh"),
        ],
        "WorkingDirectory": str(config.root),
        "EnvironmentVariables": {
            "PATH": runtime_path,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PODSEARCH_REMOTE_WORKER": worker,
            "PODSEARCH_REMOTE_REPO": remote_repo,
        },
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "remote-pull.out.log"),
        "StandardErrorPath": str(log_dir / "remote-pull.err.log"),
    }
    path = launch_agents / f"{REMOTE_PULL_LABEL}.plist"
    with path.open("wb") as output:
        plistlib.dump(payload, output, sort_keys=False)
    return path
