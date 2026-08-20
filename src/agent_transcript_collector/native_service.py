"""Shared helpers for reliably replacing native background services."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# Settings the collector only reads from the environment. A launchd/systemd
# service starts from a near-empty environment, so whatever the installing
# shell had set has to be written into the generated unit or the background
# run silently falls back to the defaults - most visibly by uploading to
# Redwood's bucket instead of the configured one.
FORWARDED_ENV_VARS = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "CTC_AWS_PROFILE",
    "CTC_S3_BUCKET",
    "CTC_S3_REGION",
    "CTC_S3_ENDPOINT_URL",
    "CTC_S3_ACCESS_KEY_ID",
    "CTC_S3_SECRET_ACCESS_KEY",
    "CTC_STORAGE_PREFIX",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "CURSOR_HOME",
    "CURSOR_USER_DATA_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_CODING_AGENT_DIR",
    "CTC_SYSTEM_PROMPT_DUMP_DIR",
    "CTC_HASH_CONCURRENCY",
    "CTC_ARCHIVE_CONCURRENCY",
    "CTC_UPLOAD_CONCURRENCY",
    "CTC_METADATA_CONCURRENCY",
    "CTC_USERNAME_STOPLIST",
)


# The system-prompt service reads bodies off disk and never talks to S3, so
# its unit carries only the settings it acts on rather than the credentials.
SYSTEM_PROMPT_ENV_VARS = (
    "CTC_SYSTEM_PROMPT_DUMP_DIR",
    "CLAUDE_CONFIG_DIR",
)


def service_environment(
    names: tuple[str, ...] = FORWARDED_ENV_VARS, **base: str
) -> dict[str, str]:
    """Return the environment a generated unit should export.

    ``CTC_S3_SECRET_ACCESS_KEY`` is among the default names, so every caller
    must write the unit with owner-only permissions.
    """
    environment = dict(base)
    environment.update(
        {name: os.environ[name] for name in names if os.environ.get(name)}
    )
    return environment


def replace_launchd_service(
    label: str,
    service_path: Path,
    *,
    run_command=subprocess.run,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.05,
) -> None:
    """Replace a per-user LaunchAgent after launchd has finished removing it."""
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    run_command(
        ["launchctl", "bootout", target],
        check=False,
        capture_output=True,
    )

    deadline = time.monotonic() + timeout_seconds
    while True:
        current = run_command(
            ["launchctl", "print", target],
            check=False,
            capture_output=True,
        )
        if current.returncode:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for launchd to remove {label}")
        time.sleep(poll_interval_seconds)

    last_error = "launchctl bootstrap failed"
    while True:
        completed = run_command(
            ["launchctl", "bootstrap", domain, str(service_path)],
            check=False,
            capture_output=True,
        )
        if completed.returncode == 0:
            return

        error = completed.stderr.decode(errors="replace").strip()
        if error:
            last_error = error
        current = run_command(
            ["launchctl", "print", target],
            check=False,
            capture_output=True,
        )
        if current.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(last_error)
        time.sleep(poll_interval_seconds)
