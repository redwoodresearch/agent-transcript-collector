"""Convert already-redacted native transcripts to canonical ATIF JSON."""

from __future__ import annotations

import json
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harbor.models.trajectories.trajectory import Trajectory

ATIF_FILENAME = "trajectory.atif.json"
ATIF_SCHEMA_VERSION = "ATIF-v1.7"
CLAUDE_FALLBACK_MODEL = "claude-opus-4-8"
CODEX_FALLBACK_MODEL = "gpt-5.5"
SUPPORTED_SOURCES = frozenset({"claude_code", "codex"})


def _serialize(trajectory: Trajectory) -> bytes:
    from harbor.models.trajectories.trajectory import Trajectory

    validated = Trajectory.model_validate(trajectory.model_dump(mode="json"))
    return (
        json.dumps(
            validated.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def conversion_manifest(
    source_id: str,
    *,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    section = {
        "status": status,
        "schema_version": ATIF_SCHEMA_VERSION,
        "converter": {"name": "harbor", "version": version("harbor")},
    }
    if status == "complete":
        section["artifact"] = ATIF_FILENAME
    if error is not None:
        section["error"] = error
    return section


def convert_redacted_transcript(
    source_id: str,
    raw: str,
    native_filename: str,
    session_id: str,
    parent_session_id: str | None = None,
) -> tuple[bytes | None, dict]:
    """Convert one redacted native transcript without reading neighboring files."""
    if source_id not in SUPPORTED_SOURCES:
        return None, conversion_manifest(source_id, status="unsupported")

    # Harbor is intentionally imported only when an archive is prepared. Status
    # checks import the archive-format constant but must remain cheap and avoid
    # loading Harbor's conversion stack.
    from harbor.agents.installed.claude_code import ClaudeCode
    from harbor.agents.installed.codex import Codex

    class ClaudeCodeImporter(ClaudeCode):
        """Avoid pricing lookups while converting a recorded session."""

        def _estimate_total_cost_from_steps(
            self, _steps: list[Any]
        ) -> float | None:
            return None

    with tempfile.TemporaryDirectory(prefix="rr-trans-atif-") as temporary:
        isolated = Path(temporary) / "session"
        isolated.mkdir(mode=0o700)
        transcript = isolated / Path(native_filename).name
        transcript.write_text(raw, encoding="utf-8")
        transcript.chmod(0o600)
        if source_id == "claude_code":
            importer = ClaudeCodeImporter(
                logs_dir=isolated,
                model_name=CLAUDE_FALLBACK_MODEL,
            )
        else:
            importer = Codex(logs_dir=isolated, model_name=CODEX_FALLBACK_MODEL)
        trajectory = importer._convert_events_to_trajectory(isolated)

    if trajectory is None:
        raise ValueError(f"Harbor produced no ATIF trajectory for {native_filename}")
    trajectory.session_id = session_id
    trajectory.trajectory_id = session_id
    trajectory.extra = {
        **(trajectory.extra or {}),
        "agent_transcript_collector": {
            "transcript_id": session_id,
            "parent_transcript_id": parent_session_id,
        },
    }
    return _serialize(trajectory), conversion_manifest(source_id, status="complete")


def derive_atif(
    source_id: str,
    raw: str,
    native_filename: str,
    session_id: str,
    parent_session_id: str | None = None,
) -> tuple[bytes | None, dict[str, Any]]:
    """Best-effort ATIF derivation that never drops the canonical transcript."""
    try:
        return convert_redacted_transcript(
            source_id, raw, native_filename, session_id, parent_session_id
        )
    except Exception as exc:  # noqa: BLE001 - ATIF is a derived archive artifact
        return None, conversion_manifest(
            source_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "ATIF_FILENAME",
    "ATIF_SCHEMA_VERSION",
    "SUPPORTED_SOURCES",
    "convert_redacted_transcript",
    "derive_atif",
]
