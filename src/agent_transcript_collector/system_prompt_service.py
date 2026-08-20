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

from .memory_injection import memory_from_request
from .native_service import (
    SYSTEM_PROMPT_ENV_VARS,
    service_environment,
    systemd_quote,
    systemd_user_dir,
)
from .system_prompt import capture_path, digest_of, stable_text

DUMP_DIR = Path(
    os.environ.get("CTC_SYSTEM_PROMPT_DUMP_DIR")
    or Path.home() / ".cache" / "agent-transcript-collector" / "api-bodies"
)
SERVICE_NAME = "agent-transcript-collector-system-prompt"
POLL_SECONDS = 2.0


def _session_id(body: dict) -> str | None:
    """Claude Code embeds the session id in the request's metadata."""
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("user_id")
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    session_id = decoded.get("session_id")
    # The id becomes a filename, so anything but a string is not usable.
    return session_id if isinstance(session_id, str) and session_id else None


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


def _store(session_id: str, text: str, memory: str | None) -> bool:
    """Keep the longest prompt and memory seen for a session.

    A session makes small side calls — naming the conversation, for one — whose
    prompts are not the agent's. The agent's is the longest by a wide margin,
    and the same holds for the injected memory that rides with it.
    """
    target = capture_path("claude_code", session_id)
    try:
        existing = json.loads(target.read_text())
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    # A session's prompt can change partway — a different model, a skill
    # loaded — so note when the substantive text differs from what was seen
    # before, ignoring the per-request header that always differs.
    variants = set(existing.get("prompt_variants") or [])
    if existing.get("text"):
        variants.add(digest_of(stable_text(existing["text"])))
    variants.add(digest_of(stable_text(text)))
    keep_prompt = len(text) > len(existing.get("text") or "")
    keep_memory = bool(memory) and len(memory or "") > len(existing.get("memory") or "")
    # A turn that adds nothing longer can still reveal that the prompt changed,
    # which is worth recording even when the stored text stays as it was.
    new_variant = variants != set(existing.get("prompt_variants") or [])
    if not keep_prompt and not keep_memory and not new_variant:
        return False
    record = {
        "session_id": session_id,
        "text": text if keep_prompt else existing.get("text", ""),
        "memory": memory if keep_memory else existing.get("memory"),
        "prompt_variants": sorted(variants),
    }
    record["sha256"] = digest_of(record["text"])
    record["chars"] = len(record["text"])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(record, indent=2))
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
                # Every reader below assumes an object; a body is whatever the
                # dump directory happens to hold.
                if isinstance(body, dict):
                    session_id = _session_id(body)
                    text = _system_text(body)
                    memory = memory_from_request(body)
                    if session_id and text and _store(session_id, text, memory):
                        kept += 1
                        if verbose:
                            print(f"kept {len(text)} chars for session {session_id}")
        # One unreadable body must not end `run`'s loop, which would leave
        # bodies accumulating unread until systemd restarts the service.
        except Exception as error:  # noqa: BLE001
            # The type only; the message could quote conversation content.
            print(f"skipped {path.name}: {type(error).__name__}", file=sys.stderr)
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


def unit_path() -> Path:
    """The unit file, where systemd actually looks for it."""
    return systemd_user_dir() / f"{SERVICE_NAME}.service"


def _is_active() -> bool:
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", f"{SERVICE_NAME}.service"],
        check=False,
    ).returncode == 0


def service_command(package_spec: str, uv_path: str = "") -> list[str]:
    from .watcher import package_command

    return package_command(package_spec, uv_path, ["system-prompts", "run"])


def status() -> dict:
    """Whether prompts are being recorded, for the UI to show."""
    unit = unit_path()
    active = unit.exists() and sys.platform.startswith("linux") and _is_active()
    try:
        settings = json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        settings = {}
    # settings.json is hand-edited, so neither it nor its env block is
    # guaranteed to be an object.
    env = settings.get("env") if isinstance(settings, dict) else None
    configured = isinstance(env, dict) and all(
        env.get(key) == value for key, value in SETTINGS_ENV.items()
    )
    return {"installed": unit.exists(), "running": active, "configured": configured}


def install(package_spec: str = "", uv_path: str = "") -> int:
    """Turn on body logging for every session and start the watcher."""
    if not sys.platform.startswith("linux"):
        print("automatic install currently supports Linux (systemd)", file=sys.stderr)
        return 1
    DUMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    unit = unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    exec_start = " ".join(
        systemd_quote(part) for part in service_command(package_spec, uv_path)
    )
    # The unit starts from a near-empty environment. Without the dump
    # directory the service would watch the default path while Claude Code
    # writes to the configured one, and raw conversation bodies would pile up
    # there unread - the exact thing this service exists to prevent.
    environment = service_environment(SYSTEM_PROMPT_ENV_VARS, HOME=str(Path.home()))
    env_lines = "".join(
        f"Environment={systemd_quote(f'{key}={value}')}\n"
        for key, value in environment.items()
    )
    unit.write_text(
        "[Unit]\n"
        "Description=Keep Claude Code system prompts, discard raw API bodies\n\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        f"{env_lines}"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    unit.chmod(0o600)
    # `enable --now` will not touch a service that is already up, so a
    # reinstall would leave the old process running with the old environment
    # and the old code. Restart is what actually adopts the unit just written.
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", f"{SERVICE_NAME}.service"],
        ["systemctl", "--user", "restart", f"{SERVICE_NAME}.service"],
    ):
        subprocess.run(argv, check=False)

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
        # An earlier install may have already switched logging on. Leaving it
        # on with the service now torn down is the disk-filling state this
        # whole dance exists to avoid, so take it back off.
        _write_settings_env(None)
        print(
            f"{SERVICE_NAME}.service did not start; raw-body logging left off",
            file=sys.stderr,
        )
        return 1

    previous_dump_dir = _configured_dump_dir()
    _write_settings_env(SETTINGS_ENV)
    # Bodies already written under a previous dump directory are whole
    # conversations that nothing will look at again once the setting moves.
    if previous_dump_dir is not None and previous_dump_dir != DUMP_DIR:
        drain(previous_dump_dir)
    print(f"started {SERVICE_NAME}.service (watching {DUMP_DIR})")
    print("New Claude Code sessions will have their system prompt recorded.")
    return 0


def _wait_until_running(seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _is_active():
            return True
        time.sleep(0.5)
    return False


def _configured_dump_dir() -> Path | None:
    """The directory the settings file currently points Claude Code at."""
    try:
        settings = json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return None
    env = settings.get("env") if isinstance(settings, dict) else None
    value = env.get("OTEL_LOG_RAW_API_BODIES") if isinstance(env, dict) else None
    if not isinstance(value, str) or not value.startswith("file:"):
        return None
    return Path(value[len("file:"):])


def _write_settings_env(values: dict[str, str] | None) -> None:
    """Add or remove the env block Claude Code reads to enable body logging."""
    settings_path = _settings_path()
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    if not isinstance(settings.get("env"), dict):
        settings.pop("env", None)
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
    unit_path().unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    # Where the settings said to dump is the truth about where bodies landed;
    # this process's own environment need not agree with the shell that
    # installed it.
    dumped = {DUMP_DIR, _configured_dump_dir()} - {None}
    _write_settings_env(None)
    print(f"removed raw-body logging from {_settings_path()}")
    # Anything still dumped is conversation content; do not leave it behind.
    for directory in sorted(dumped):
        drain(directory)
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
