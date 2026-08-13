"""Install and manage the always-on local transcript review UI."""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .native_service import replace_launchd_service
from .paths import installation_lock_path, ui_log_path, watcher_config_path

PACKAGE_NAME = "agent-transcript-collector"
PACKAGE_SPEC = (
    "git+https://github.com/redwoodresearch/agent-transcript-collector@main"
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8123
LAUNCHD_LABEL = "com.redwoodresearch.agent-transcript-collector.ui"
SYSTEMD_NAME = "agent-transcript-collector-ui"
FORWARDED_ENV_VARS = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "CTC_AWS_PROFILE",
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


def _find_uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install the CLI and background UI")
    return uv


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


@contextmanager
def _installation_lock():
    path = installation_lock_path()
    _ensure_private_dir(path.parent)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _environment() -> dict[str, str]:
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    environment.update(
        {name: os.environ[name] for name in FORWARDED_ENV_VARS if os.environ.get(name)}
    )
    return environment


def server_command(uv_path: str | None = None) -> list[str]:
    """Run the UI from main, refreshing the checkout before every launch."""
    return [
        uv_path or _find_uv(),
        "tool",
        "run",
        "--refresh-package",
        PACKAGE_NAME,
        "--from",
        PACKAGE_SPEC,
        "rr-trans",
        "ui",
        "--host",
        DEFAULT_HOST,
        "--port",
        str(DEFAULT_PORT),
        "--no-open",
        "--strict-port",
    ]


def launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "systemd" / "user" / f"{SYSTEMD_NAME}.service"


def render_launchd(uv_path: str | None = None) -> bytes:
    log = ui_log_path()
    _ensure_private_dir(log.parent)
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": server_command(uv_path),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "EnvironmentVariables": _environment(),
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        },
        sort_keys=True,
    )


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(uv_path: str | None = None) -> bytes:
    log = ui_log_path()
    _ensure_private_dir(log.parent)
    command = " ".join(_systemd_quote(arg) for arg in server_command(uv_path))
    env_lines = "\n".join(
        f"Environment={_systemd_quote(f'{key}={value}')}"
        for key, value in _environment().items()
    )
    return (
        "[Unit]\n"
        "Description=Agent transcript collector review UI\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        f"{env_lines}\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def install_service(
    *, platform: str | None = None, run_command=subprocess.run
) -> dict:
    """Install or replace the native per-user UI service and start it."""
    platform = platform or sys.platform
    uv_path = _find_uv()
    if platform == "darwin":
        service_path = launchd_path()
        _atomic_write(service_path, render_launchd(uv_path))
        try:
            replace_launchd_service(
                LAUNCHD_LABEL,
                service_path,
                run_command=run_command,
            )
        except Exception:
            service_path.unlink(missing_ok=True)
            raise
    elif platform.startswith("linux"):
        service_path = systemd_path()
        _atomic_write(service_path, render_systemd(uv_path))
        try:
            run_command(["systemctl", "--user", "daemon-reload"], check=True)
            run_command(
                ["systemctl", "--user", "enable", "--now", service_path.name],
                check=True,
            )
            run_command(
                ["systemctl", "--user", "restart", service_path.name], check=True
            )
        except Exception:
            service_path.unlink(missing_ok=True)
            run_command(["systemctl", "--user", "daemon-reload"], check=False)
            raise
    else:
        raise RuntimeError("the background UI supports macOS and Linux")
    return status(platform=platform, run_command=run_command)


def install_refreshed_services(run_command=subprocess.run) -> dict:
    """Install the UI and refresh the watcher when upload consent exists."""
    ui = install_service(run_command=run_command)
    config_path = watcher_config_path()
    if config_path.exists():
        from .watcher import install as install_watcher
        from .watcher import load_config

        watcher = install_watcher(load_config(config_path), run_command=run_command)
        watcher["configured"] = True
    else:
        watcher = {
            "configured": False,
            "installed": False,
            "skipped": "automatic uploads have not been configured",
        }
    return {**ui, "ui": ui, "watcher": watcher}


def install_and_update(run_command=subprocess.run) -> dict:
    """Refresh the CLI, then install all configured services from that version."""
    uv_path = _find_uv()
    with _installation_lock():
        run_command(
            [uv_path, "tool", "install", "--force", "--refresh", PACKAGE_SPEC],
            check=True,
        )
        completed = run_command(
            [
                uv_path,
                "tool",
                "run",
                "--refresh-package",
                PACKAGE_NAME,
                "--from",
                PACKAGE_SPEC,
                "rr-trans",
                "ui-service",
                "install",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                "updated CLI could not install background services"
                + (f": {detail}" if detail else "")
            )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "updated CLI returned an invalid background-service result: "
            f"{completed.stdout.strip()}"
        ) from exc
    # The service's uv process can only acquire its package cache after the
    # nested installer above exits. Give it a short window to become reachable
    # so a successful setup command normally confirms the complete outcome.
    for _ in range(100):
        if _port_is_open():
            result["running"] = True
            break
        time.sleep(0.2)
    return result


def uninstall(
    *, platform: str | None = None, run_command=subprocess.run
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
        service_path = systemd_path()
        run_command(
            ["systemctl", "--user", "disable", "--now", service_path.name],
            check=False,
        )
        service_path.unlink(missing_ok=True)
        run_command(["systemctl", "--user", "daemon-reload"], check=False)
    else:
        raise RuntimeError("the background UI supports macOS and Linux")
    return {"installed": False, "url": f"http://localhost:{DEFAULT_PORT}"}


def _port_is_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def status(
    *, platform: str | None = None, run_command=subprocess.run
) -> dict:
    platform = platform or sys.platform
    if platform == "darwin":
        service_path = launchd_path()
        completed = run_command(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            check=False,
            capture_output=True,
        )
        installed = service_path.exists() and completed.returncode == 0
    elif platform.startswith("linux"):
        service_path = systemd_path()
        completed = run_command(
            ["systemctl", "--user", "is-active", "--quiet", service_path.name],
            check=False,
        )
        installed = service_path.exists() and completed.returncode == 0
    else:
        service_path = None
        installed = False
    return {
        "installed": installed,
        "running": installed and _port_is_open(),
        "url": f"http://localhost:{DEFAULT_PORT}",
        "service_file": str(service_path) if service_path else None,
        "log_file": str(ui_log_path()),
        "update_source": PACKAGE_SPEC,
    }
