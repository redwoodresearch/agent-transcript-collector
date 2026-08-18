"""Keep the system prompt from every Claude Code session, and nothing else.

Claude Code can dump raw API request bodies to a directory (see `install`
below). Those bodies are the only place its system prompt appears, but each one
also contains the whole conversation so far and is written per turn, so left
alone the directory grows without bound.

This watcher turns that firehose into a few kilobytes per session: every body
it sees is read once, its system prompt filed under the session id the body
carries in `metadata.user_id`, and the body deleted immediately. Nothing else
is retained, and the conversation content never leaves the temporary directory.

Enabling automatic uploads installs this; disabling removes it. It can also be
driven directly:

    rr-trans system-prompts run       # watch until stopped
    rr-trans system-prompts drain     # process what is there, exit
    rr-trans system-prompts install   # settings + background service
    rr-trans system-prompts uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .system_prompt import capture_path, digest_of

DUMP_DIR = Path(
    os.environ.get("CTC_SYSTEM_PROMPT_DUMP_DIR")
    or Path.home() / ".cache" / "agent-transcript-collector" / "api-bodies"
)
SERVICE_NAME = "agent-transcript-collector-system-prompt"
POLL_SECONDS = 2.0


def _session_id(body: dict) -> str | None:
    """Claude Code embeds the session id in the request's metadata."""
    raw = (body.get("metadata") or {}).get("user_id")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw).get("session_id")
    except ValueError:
        return None


def _system_text(body: dict) -> str | None:
    system = body.get("system")
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        parts = [
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n\n".join(part for part in parts if part) or None
    return None


def _store(session_id: str, text: str) -> bool:
    """Keep the longest prompt seen for a session.

    A session makes small side calls — naming the conversation, for one — whose
    prompts are not the agent's. The agent's is the longest by a wide margin.
    """
    target = capture_path("claude_code", session_id)
    try:
        existing = json.loads(target.read_text())
        if len(existing.get("text") or "") >= len(text):
            return False
    except (OSError, ValueError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps({
        "session_id": session_id,
        "sha256": digest_of(text),
        "chars": len(text),
        "text": text,
    }, indent=2))
    target.chmod(0o600)
    return True


def drain(directory: Path = DUMP_DIR, *, verbose: bool = False) -> int:
    """Read and delete every body currently in the dump directory."""
    kept = 0
    if not directory.exists():
        return 0
    for path in sorted(directory.glob("*.json")):
        try:
            if path.name.endswith(".request.json"):
                body = json.loads(path.read_text())
                session_id = _session_id(body)
                text = _system_text(body)
                if session_id and text and _store(session_id, text):
                    kept += 1
                    if verbose:
                        print(f"kept {len(text)} chars for session {session_id}")
        except (OSError, ValueError):
            pass
        finally:
            # Bodies hold whole conversations; they are never kept.
            try:
                path.unlink()
            except OSError:
                pass
    return kept


def run(directory: Path = DUMP_DIR) -> int:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f"watching {directory}", flush=True)
    while True:
        drain(directory, verbose=True)
        time.sleep(POLL_SECONDS)


SETTINGS_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_LOG_RAW_API_BODIES": f"file:{DUMP_DIR}",
}


def _settings_path() -> Path:
    return Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    ) / "settings.json"


def service_command(package_spec: str, uv_path: str = "") -> list[str]:
    from .watcher import package_command

    return package_command(package_spec, uv_path, ["system-prompts", "run"])


def status() -> dict:
    """Whether prompts are being recorded, for the UI to show."""
    unit = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    active = False
    if unit.exists() and sys.platform.startswith("linux"):
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", f"{SERVICE_NAME}.service"],
            check=False,
        ).returncode == 0
    try:
        settings = json.loads(_settings_path().read_text())
        configured = all(
            settings.get("env", {}).get(key) == value
            for key, value in SETTINGS_ENV.items()
        )
    except (OSError, ValueError):
        configured = False
    return {"installed": unit.exists(), "running": active, "configured": configured}


def install(package_spec: str = "", uv_path: str = "") -> int:
    """Turn on body logging for every session and start the watcher."""
    if not sys.platform.startswith("linux"):
        print("automatic install currently supports Linux (systemd)", file=sys.stderr)
        return 1
    DUMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / f"{SERVICE_NAME}.service"
    command = " ".join(f'"{part}"' for part in service_command(package_spec, uv_path))
    unit.write_text(
        "[Unit]\n"
        "Description=Keep Claude Code system prompts, discard raw API bodies\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        f'Environment="HOME={Path.home()}"\n'
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"],
    ):
        subprocess.run(command, check=False)

    # Logging is only switched on once something is known to be consuming it.
    # Raw bodies are large and written per turn, so enabling them with a dead
    # watcher fills the disk with conversation content nobody reads.
    if not _wait_until_running():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"],
            check=False,
        )
        unit.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print(
            f"{SERVICE_NAME}.service did not start; raw-body logging left off",
            file=sys.stderr,
        )
        return 1

    _write_settings_env(SETTINGS_ENV)
    print(f"started {SERVICE_NAME}.service (watching {DUMP_DIR})")
    print("New Claude Code sessions will have their system prompt recorded.")
    return 0


def _wait_until_running(seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if status().get("running"):
            return True
        time.sleep(0.5)
    return False


def _write_settings_env(values: dict[str, str] | None) -> None:
    """Add or remove the env block Claude Code reads to enable body logging."""
    settings_path = _settings_path()
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        settings = {}
    if values:
        settings.setdefault("env", {}).update(values)
    else:
        for key in SETTINGS_ENV:
            settings.get("env", {}).pop(key, None)
        if not settings.get("env"):
            settings.pop("env", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def uninstall() -> int:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"],
        check=False,
    )
    (Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service").unlink(
        missing_ok=True
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    _write_settings_env(None)
    print(f"removed raw-body logging from {_settings_path()}")
    # Anything still dumped is conversation content; do not leave it behind.
    drain()
    print("stopped; remaining API bodies discarded")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "drain", "install", "uninstall", "status"))
    args = parser.parse_args((argv or sys.argv)[1:])
    if args.action == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if args.action == "run":
        return run()
    if args.action == "drain":
        print(f"kept {drain(verbose=True)} prompt(s)")
        return 0
    return install() if args.action == "install" else uninstall()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
