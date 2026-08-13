"""Shared helpers for reliably replacing native background services."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


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
