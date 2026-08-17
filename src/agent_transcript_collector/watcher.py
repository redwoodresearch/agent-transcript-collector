"""Persisted consent and native automatic watcher installation."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .native_service import replace_launchd_service
from .paths import (
    log_path,
    watcher_config_path,
    watcher_state_path,
)
from .s3client import make_s3_client
from .scan import ScanResult, scan_transcripts
from .sources.base import project_identity
from .upload_status import refresh_upload_status
from .upload_workflow import prepare_uploads, record_uploaded, upload_candidates
from .uploader import (
    UploadBusy,
    UploadLock,
    upload_artifacts,
)

PACKAGE_SPEC = "git+https://github.com/redwoodresearch/agent-transcript-collector@main"
PACKAGE_NAME = "agent-transcript-collector"
LAUNCHD_LABEL = "com.redwoodresearch.agent-transcript-collector"
SYSTEMD_NAME = "agent-transcript-collector"
WATCH_INTERVAL_SECONDS = 60 * 60
AUTO_UPLOADER_VERSION = 9
SOURCE_ENV_VARS = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "CURSOR_HOME",
    "CURSOR_USER_DATA_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_CODING_AGENT_DIR",
    "CTC_HASH_CONCURRENCY",
    "CTC_ARCHIVE_CONCURRENCY",
    "CTC_UPLOAD_CONCURRENCY",
    "CTC_METADATA_CONCURRENCY",
    "CTC_USERNAME_STOPLIST",
)


@dataclass(frozen=True)
class AllowedProject:
    identity: str
    label: str = ""


@dataclass
class WatcherConfig:
    schema_version: int = 2
    auto_uploader_version: int = AUTO_UPLOADER_VERSION
    contributor: str = "anonymous"
    aws_profile: str = "rw-eng"
    all_projects: bool = False
    projects: list[AllowedProject] = field(default_factory=list)
    excluded_projects: list[AllowedProject] = field(default_factory=list)
    source_env: dict[str, str] = field(default_factory=dict)
    package_spec: str = PACKAGE_SPEC
    uv_path: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> WatcherConfig:
        if data.get("schema_version") != 2:
            raise ValueError("unsupported watcher configuration version")
        projects = sorted(
            {
                AllowedProject(
                    identity=str(item["identity"]),
                    label=str(item.get("label", "")),
                )
                for item in data.get("projects", [])
                if item.get("identity")
            },
            key=lambda item: (item.identity, item.label),
        )
        return cls(
            schema_version=2,
            auto_uploader_version=int(data.get("auto_uploader_version", 0)),
            contributor=str(data.get("contributor", "anonymous")),
            aws_profile=str(data.get("aws_profile", "rw-eng")),
            all_projects=bool(data.get("all_projects", False)),
            projects=projects,
            excluded_projects=sorted(
                {
                    AllowedProject(
                        identity=str(item["identity"]),
                        label=str(item.get("label", "")),
                    )
                    for item in data.get("excluded_projects", [])
                    if item.get("identity")
                },
                key=lambda item: (item.identity, item.label),
            ),
            source_env={
                str(key): str(value)
                for key, value in data.get("source_env", {}).items()
                if key in SOURCE_ENV_VARS
            },
            package_spec=str(data.get("package_spec", PACKAGE_SPEC)),
            uv_path=str(data.get("uv_path", "")),
        )


# =========== MIGRATIONS ============

def _migrate_config(data: dict) -> tuple[dict, bool]:
    """Convert old source/group selections to current project identities once."""
    version = data.get("schema_version")
    if version == 2 and not any(
        item.get("members") for item in data.get("projects", [])
    ):
        return data, False
    if version not in {1, 2}:
        raise ValueError("unsupported watcher configuration version")

    members = [
        (str(item.get("source", "")), str(item.get("group", "")))
        for item in data.get("groups", [])
    ] if version == 1 else [
        (str(member.get("source", "")), str(member.get("group", "")))
        for project in data.get("projects", [])
        for member in project.get("members", [])
    ]
    wanted = {(source, group) for source, group in members if source and group}
    source_env = {
        str(key): str(value)
        for key, value in data.get("source_env", {}).items()
        if key in SOURCE_ENV_VARS
    }
    old_source_env = {name: os.environ.get(name) for name in source_env}
    os.environ.update(source_env)
    try:
        scan = scan_transcripts()
    finally:
        for name, value in old_source_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    matched: set[tuple[str, str]] = set()
    migrated: dict[str, AllowedProject] = {}
    for project in scan.projects:
        if project.directory is None:
            continue
        old_group, _label = project_identity(project.directory)
        project_members = {
            (transcript.source, old_group) for transcript in project.transcripts
        }
        if project_members & wanted:
            matched.update(project_members & wanted)
            migrated[project.identity] = AllowedProject(project.identity, project.label)
    if matched != wanted:
        raise ValueError(
            "an old watcher selection cannot be mapped to a current project; "
            "open the UI and select its project again"
        )
    if version == 2:
        for item in data.get("projects", []):
            if item.get("identity") and not item.get("members"):
                identity = str(item["identity"])
                migrated[identity] = AllowedProject(
                    identity, str(item.get("label", ""))
                )
    result = dict(data)
    result["schema_version"] = 2
    result.pop("groups", None)
    result["projects"] = [
        {"identity": project.identity, "label": project.label}
        for project in sorted(migrated.values(), key=lambda item: item.identity)
    ]
    return result, True


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
    data = {
        "schema_version": config.schema_version,
        "auto_uploader_version": config.auto_uploader_version,
        "contributor": config.contributor,
        "aws_profile": config.aws_profile,
        "all_projects": config.all_projects,
        "projects": [
            {"identity": project.identity, "label": project.label}
            for project in config.projects
        ],
        "excluded_projects": [
            {"identity": project.identity, "label": project.label}
            for project in config.excluded_projects
        ],
        "source_env": config.source_env,
        "package_spec": config.package_spec,
        "uv_path": config.uv_path,
    }
    payload = json.dumps(data, indent=2, sort_keys=True).encode() + b"\n"
    _atomic_write(target, payload)
    return target


def load_config(path: Path | None = None) -> WatcherConfig:
    target = path or watcher_config_path()
    data, migrated = _migrate_config(json.loads(target.read_text()))
    config = WatcherConfig.from_dict(data)
    if migrated:
        save_config(config, target)
    return config


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


def selected_project_identities(
    scan: ScanResult, config: WatcherConfig
) -> set[str]:
    """Resolve project consent from one discovery snapshot."""
    available = {project.identity for project in scan.projects}
    if config.all_projects:
        # Everything on this machine, minus the projects opted out one by one.
        return available - {project.identity for project in config.excluded_projects}
    return {project.identity for project in config.projects} & available


def discover_allowed(config: WatcherConfig):
    scan = scan_transcripts()
    return scan.sessions_for_projects(selected_project_identities(scan, config))


def _sso_hint(exc: Exception, profile: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    lower = message.lower()
    if "sso" in lower or "token" in lower or "credential" in lower:
        return f"{message}. Run: aws sso login --profile {profile}"
    return message


def run_once(
    config: WatcherConfig,
    *,
    s3: Any | None = None,
    state_path: Path | None = None,
    lock_path: Path | None = None,
    run_command=subprocess.run,
) -> dict:
    """Run the shared pipeline and upload its ready artifacts."""
    previous_state = load_state(state_path)
    started = datetime.now(timezone.utc).isoformat()
    result = {
        "started_at": started,
        "finished_at": None,
        "status": "running",
        "sessions_uploaded": 0,
        "errors": [],
    }
    previous_revision = previous_state.get("cli_revision")
    cli_update = update_cli(
        config,
        previous_revision=previous_revision,
        run_command=run_command,
    )
    result["cli_update"] = cli_update
    if cli_update.get("revision"):
        result["cli_revision"] = cli_update["revision"]
    elif previous_revision:
        result["cli_revision"] = previous_revision
    old_profile = os.environ.get("CTC_AWS_PROFILE")
    old_source_env = {name: os.environ.get(name) for name in config.source_env}
    os.environ["CTC_AWS_PROFILE"] = config.aws_profile
    os.environ.update(config.source_env)
    try:
        with UploadLock(lock_path):
            client = s3 or make_s3_client()
            selections = discover_allowed(config)
            pipeline = refresh_upload_status(
                selections, config.contributor, s3=client
            )
            result["errors"].extend(
                item.get("error", str(item)) for item in pipeline["errors"]
            )
            candidates, stale = upload_candidates(selections, config.contributor)
            if stale:
                result["errors"].append(
                    f"{len(stale)} transcript(s) require another refresh"
                )
            with tempfile.TemporaryDirectory(prefix="ctc-upload-") as directory:
                artifacts, preparation_errors = prepare_uploads(
                    selections, candidates, config.contributor, directory
                )
                result["errors"].extend(
                    item.get("error", str(item)) for item in preparation_errors
                )
                uploads, upload_errors = upload_artifacts(client, artifacts)
            successful = {
                (item.get("source"), item.get("project"), item.get("parent") or "",
                 item.get("session"))
                for item in uploads
            }
            uploaded_artifacts = [
                item for item in artifacts
                if (item.get("source"), item.get("project"),
                    item.get("parent") or "", item.get("session")) in successful
            ]
            if uploaded_artifacts:
                record_uploaded(uploaded_artifacts, config.contributor)
            result["sessions_uploaded"] = sum(
                item["transcript_count"] for item in uploads
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


def _running_revision() -> str:
    """Return the Git revision backing the currently running package."""
    try:
        direct_url = importlib.metadata.distribution(PACKAGE_NAME).read_text(
            "direct_url.json"
        )
        data = json.loads(direct_url or "{}")
        return str(data.get("vcs_info", {}).get("commit_id", ""))
    except (
        AttributeError,
        importlib.metadata.PackageNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        return ""


def update_cli(
    config: WatcherConfig,
    *,
    previous_revision: str | None,
    run_command=subprocess.run,
) -> dict:
    """Install the refreshed watcher package when it advances to a new commit."""
    revision = _running_revision()
    if not revision:
        return {
            "status": "skipped",
            "reason": "The running package does not expose a Git revision",
        }
    if revision == previous_revision:
        return {"status": "current", "revision": revision}

    try:
        uv = config.uv_path or _find_uv()
        completed = run_command(
            [uv, "tool", "install", "--force", "--refresh", config.package_spec],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {
            "status": "failed",
            "revision": previous_revision or "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        return {
            "status": "failed",
            "revision": previous_revision or "",
            "error": detail or f"uv tool install exited with {completed.returncode}",
        }
    return {"status": "updated", "revision": revision}


def watcher_command(config: WatcherConfig, config_path: Path) -> list[str]:
    uv = config.uv_path or _find_uv()
    return [
        uv,
        "tool",
        "run",
        "--refresh-package",
        "agent-transcript-collector",
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
        "Description=Upload accepted coding-agent transcripts every hour\n\n"
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
    # Scheduled installs must follow the supported release branch. Persisting a
    # development-branch package spec can permanently break the watcher once
    # that temporary branch is removed.
    config.package_spec = PACKAGE_SPEC
    config.uv_path = config.uv_path or _find_uv()
    target = save_config(config, config_path)
    if platform == "darwin":
        service_path = launchd_path()
        _atomic_write(service_path, render_launchd(config, target))
        try:
            replace_launchd_service(
                LAUNCHD_LABEL,
                service_path,
                run_command=run_command,
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
            run_command(
                ["systemctl", "--user", "restart", timer_path.name], check=True
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


def status(
    platform: str | None = None,
    run_command=subprocess.run,
) -> dict:
    platform = platform or sys.platform
    config_path = watcher_config_path()
    if platform == "darwin":
        service_files = [launchd_path()]
        active = False
        running = False
        if all(path.exists() for path in service_files):
            completed = run_command(
                ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                check=False,
                capture_output=True,
            )
            active = completed.returncode == 0
            running = active and b"state = running" in completed.stdout
    elif platform.startswith("linux"):
        service_files = list(systemd_paths())
        active = False
        running = False
        if all(path.exists() for path in service_files):
            enabled = run_command(
                ["systemctl", "--user", "is-enabled", "--quiet", f"{SYSTEMD_NAME}.timer"],
                check=False,
            )
            timer_active = run_command(
                ["systemctl", "--user", "is-active", "--quiet", f"{SYSTEMD_NAME}.timer"],
                check=False,
            )
            active = enabled.returncode == 0 and timer_active.returncode == 0
            service = run_command(
                ["systemctl", "--user", "is-active", "--quiet", f"{SYSTEMD_NAME}.service"],
                check=False,
            )
            running = service.returncode == 0
    else:
        service_files = []
        active = False
        running = False
    service_files_present = bool(service_files) and all(
        path.exists() for path in service_files
    )
    result = {
        "installed": active,
        "running": running,
        "service_files_present": service_files_present,
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
                "all_projects": config.all_projects,
                "excluded_projects": [
                    {"identity": project.identity, "label": project.label}
                    for project in config.excluded_projects
                ],
                "projects": [
                    {"identity": project.identity, "label": project.label}
                    for project in config.projects
                ],
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
