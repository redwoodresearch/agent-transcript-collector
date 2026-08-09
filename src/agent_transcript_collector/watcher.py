"""Persisted consent and native automatic watcher installation."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import (
    log_path,
    watcher_config_path,
    watcher_state_path,
)
from .s3client import make_s3_client
from .sources import SOURCES
from .uploader import (
    UploadBusy,
    UploadLock,
    list_uploaded_keys,
    upload_transcripts,
)

PACKAGE_SPEC = "git+https://github.com/redwoodresearch/agent-transcript-collector@main"
LAUNCHD_LABEL = "com.redwoodresearch.agent-transcript-collector"
SYSTEMD_NAME = "agent-transcript-collector"
WATCH_INTERVAL_SECONDS = 60
AUTO_UPLOADER_VERSION = 2
SOURCE_ENV_VARS = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "CURSOR_HOME",
    "CURSOR_USER_DATA_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_CODING_AGENT_DIR",
)


@dataclass(frozen=True)
class AllowedGroup:
    source: str
    group: str
    label: str = ""


@dataclass
class WatcherConfig:
    schema_version: int = 1
    auto_uploader_version: int = AUTO_UPLOADER_VERSION
    contributor: str = "anonymous"
    aws_profile: str = "rw-eng"
    groups: list[AllowedGroup] = field(default_factory=list)
    source_env: dict[str, str] = field(default_factory=dict)
    package_spec: str = PACKAGE_SPEC
    uv_path: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "WatcherConfig":
        if data.get("schema_version") != 1:
            raise ValueError("unsupported watcher configuration version")
        groups = [
            AllowedGroup(
                source=str(item["source"]),
                group=str(item["group"]),
                label=str(item.get("label", "")),
            )
            for item in data.get("groups", [])
        ]
        return cls(
            schema_version=1,
            auto_uploader_version=int(data.get("auto_uploader_version", 0)),
            contributor=str(data.get("contributor", "anonymous")),
            aws_profile=str(data.get("aws_profile", "rw-eng")),
            groups=groups,
            source_env={
                str(key): str(value)
                for key, value in data.get("source_env", {}).items()
                if key in SOURCE_ENV_VARS
            },
            package_spec=str(data.get("package_spec", PACKAGE_SPEC)),
            uv_path=str(data.get("uv_path", "")),
        )


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    _ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_config(config: WatcherConfig, path: Path | None = None) -> Path:
    target = path or watcher_config_path()
    payload = json.dumps(asdict(config), indent=2, sort_keys=True).encode() + b"\n"
    _atomic_write(target, payload)
    return target


def load_config(path: Path | None = None) -> WatcherConfig:
    target = path or watcher_config_path()
    return WatcherConfig.from_dict(json.loads(target.read_text()))


def save_state(state: dict, path: Path | None = None) -> None:
    target = path or watcher_state_path()
    _atomic_write(
        target,
        (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(),
    )


def load_state(path: Path | None = None) -> dict:
    target = path or watcher_state_path()
    try:
        return json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def capture_source_env() -> dict[str, str]:
    return {name: os.environ[name] for name in SOURCE_ENV_VARS if os.environ.get(name)}


def _resolve_allowed(config: WatcherConfig):
    """Pair configured consent with exact current source-group keys."""
    allowed = {(item.source, item.group) for item in config.groups}
    return [
        (source, group)
        for source in SOURCES
        for group in source.discover()
        if (source.id, group.key) in allowed
    ]


def discover_allowed(config: WatcherConfig):
    sessions_by_source = {}
    for source, group in _resolve_allowed(config):
        entry = sessions_by_source.setdefault(source.id, (source, []))
        entry[1].extend(group.sessions)
    return list(sessions_by_source.values())


def _sso_hint(exc: Exception, profile: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    lower = message.lower()
    if "sso" in lower or "token" in lower or "credential" in lower:
        return f"{message}. Run: aws sso login --profile {profile}"
    return message


def run_once(
    config: WatcherConfig,
    *,
    s3=None,
    state_path: Path | None = None,
    lock_path: Path | None = None,
) -> dict:
    """Upload all new content versions from explicitly allowed groups."""
    previous_state = load_state(state_path)
    started = datetime.now(timezone.utc).isoformat()
    result = {
        "started_at": started,
        "finished_at": None,
        "status": "running",
        "sessions_uploaded": 0,
        "errors": [],
    }
    old_profile = os.environ.get("CTC_AWS_PROFILE")
    old_source_env = {name: os.environ.get(name) for name in config.source_env}
    os.environ["CTC_AWS_PROFILE"] = config.aws_profile
    os.environ.update(config.source_env)
    try:
        with UploadLock(lock_path):
            client = s3 or make_s3_client()
            uploaded_keys = list_uploaded_keys(client, config.contributor)
            for source, sessions in discover_allowed(config):
                uploaded, upload_errors = upload_transcripts(
                    client,
                    source,
                    sessions,
                    config.contributor,
                    uploaded_keys=uploaded_keys,
                )
                result["sessions_uploaded"] += sum(
                    item["transcript_count"] for item in uploaded
                )
                result["errors"].extend(
                    item.get("error", str(item)) for item in upload_errors
                )
            result["status"] = "partial" if result["errors"] else "completed"
    except UploadBusy as exc:
        result["status"] = "skipped"
        result["errors"].append(str(exc))
    except Exception as exc:
        result["status"] = "failed"
        result["errors"].append(_sso_hint(exc, config.aws_profile))
    finally:
        if old_profile is None:
            os.environ.pop("CTC_AWS_PROFILE", None)
        else:
            os.environ["CTC_AWS_PROFILE"] = old_profile
        for name, value in old_source_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        if result["sessions_uploaded"]:
            result["last_uploaded_at"] = result["finished_at"]
        elif previous_state.get("last_uploaded_at"):
            result["last_uploaded_at"] = previous_state["last_uploaded_at"]
        save_state(result, state_path)
    return result


def _find_uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install the automatic watcher")
    return uv


def watcher_command(config: WatcherConfig, config_path: Path) -> list[str]:
    uv = config.uv_path or _find_uv()
    return [
        uv,
        "tool",
        "run",
        "--from",
        config.package_spec,
        "rr-trans",
        "watcher",
        "run",
        "--config",
        str(config_path),
    ]


def launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    user = root / "systemd" / "user"
    return user / f"{SYSTEMD_NAME}.service", user / f"{SYSTEMD_NAME}.timer"


def render_launchd(config: WatcherConfig, config_path: Path) -> bytes:
    log = log_path()
    _ensure_private_dir(log.parent)
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": watcher_command(config, config_path),
        "StartInterval": WATCH_INTERVAL_SECONDS,
        "RunAtLoad": True,
        "EnvironmentVariables": environment,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(config: WatcherConfig, config_path: Path) -> tuple[bytes, bytes]:
    command = " ".join(
        _systemd_quote(arg) for arg in watcher_command(config, config_path)
    )
    environment = {
        "HOME": str(Path.home()),
    }
    env_lines = "\n".join(
        f"Environment={_systemd_quote(f'{key}={value}')}"
        for key, value in environment.items()
    )
    service = (
        "[Unit]\n"
        "Description=Upload accepted coding-agent transcripts\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={command}\n"
        f"{env_lines}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Upload accepted coding-agent transcripts every minute\n\n"
        "[Timer]\n"
        "OnBootSec=0\n"
        f"OnUnitActiveSec={WATCH_INTERVAL_SECONDS}s\n"
        "AccuracySec=1s\n"
        "Persistent=true\n"
        f"Unit={SYSTEMD_NAME}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service.encode(), timer.encode()


def install(
    config: WatcherConfig,
    *,
    config_path: Path | None = None,
    platform: str | None = None,
    run_command=subprocess.run,
) -> dict:
    """Persist consent and install/reload the native per-user timer.

    Activation failures roll the unit files back, because `status` reads
    installed state from their presence and would otherwise report a scheduled job
    that never loaded.
    """
    platform = platform or sys.platform
    config.auto_uploader_version = AUTO_UPLOADER_VERSION
    config.uv_path = config.uv_path or _find_uv()
    target = save_config(config, config_path)
    if platform == "darwin":
        service_path = launchd_path()
        _atomic_write(service_path, render_launchd(config, target))
        domain = f"gui/{os.getuid()}"
        try:
            run_command(
                ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                check=False,
                capture_output=True,
            )
            completed = run_command(
                ["launchctl", "bootstrap", domain, str(service_path)],
                check=False,
                capture_output=True,
            )
            if completed.returncode:
                raise RuntimeError(
                    completed.stderr.decode(errors="replace")
                    or "launchctl bootstrap failed"
                )
        except Exception:
            service_path.unlink(missing_ok=True)
            raise
        files = [str(service_path)]
    elif platform.startswith("linux"):
        service_path, timer_path = systemd_paths()
        service, timer = render_systemd(config, target)
        _atomic_write(service_path, service)
        _atomic_write(timer_path, timer)
        try:
            run_command(["systemctl", "--user", "daemon-reload"], check=True)
            run_command(
                ["systemctl", "--user", "enable", "--now", timer_path.name], check=True
            )
        except Exception:
            service_path.unlink(missing_ok=True)
            timer_path.unlink(missing_ok=True)
            run_command(["systemctl", "--user", "daemon-reload"], check=False)
            raise
        files = [str(service_path), str(timer_path)]
    else:
        raise RuntimeError("the watcher installer supports macOS and Linux")
    return {"installed": True, "config_path": str(target), "service_files": files}


def uninstall(
    *,
    platform: str | None = None,
    purge: bool = False,
    run_command=subprocess.run,
) -> dict:
    platform = platform or sys.platform
    if platform == "darwin":
        domain = f"gui/{os.getuid()}"
        run_command(
            ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
            check=False,
            capture_output=True,
        )
        launchd_path().unlink(missing_ok=True)
    elif platform.startswith("linux"):
        service_path, timer_path = systemd_paths()
        run_command(
            ["systemctl", "--user", "disable", "--now", timer_path.name],
            check=False,
        )
        service_path.unlink(missing_ok=True)
        timer_path.unlink(missing_ok=True)
        run_command(["systemctl", "--user", "daemon-reload"], check=False)
    else:
        raise RuntimeError("the watcher installer supports macOS and Linux")
    if purge:
        watcher_config_path().unlink(missing_ok=True)
        watcher_state_path().unlink(missing_ok=True)
    return {"installed": False}


def status(platform: str | None = None) -> dict:
    platform = platform or sys.platform
    config_path = watcher_config_path()
    if platform == "darwin":
        service_files = [launchd_path()]
    elif platform.startswith("linux"):
        service_files = list(systemd_paths())
    else:
        service_files = []
    result = {
        "installed": bool(service_files)
        and all(path.exists() for path in service_files),
        "configured": config_path.exists(),
        "current_version": AUTO_UPLOADER_VERSION,
        "interval_seconds": WATCH_INTERVAL_SECONDS,
        "state": load_state(),
    }
    if config_path.exists():
        try:
            config = load_config(config_path)
            result["config"] = {
                "auto_uploader_version": config.auto_uploader_version,
                "contributor": config.contributor,
                "groups": [asdict(group) for group in config.groups],
                "aws_profile": config.aws_profile,
            }
            result["needs_reinstall"] = (
                result["installed"]
                and config.auto_uploader_version != AUTO_UPLOADER_VERSION
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result["error"] = f"Invalid watcher configuration: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the transcript watcher")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", type=Path, default=watcher_config_path())
    subparsers.add_parser("status")
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--purge", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_once(load_config(args.config))
        print(json.dumps(result))
        return 0 if result["status"] in {"completed", "skipped"} else 1
    if args.command == "status":
        print(json.dumps(status(), indent=2))
        return 0
    uninstall(purge=args.purge)
    print("Watcher uninstalled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
