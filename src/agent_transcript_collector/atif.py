"""Convert already-redacted native transcripts to canonical ATIF JSON."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterable
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from harbor.models.trajectories.trajectory import Trajectory

ATIF_FILENAME = "trajectory.atif.json"
ATIF_SCHEMA_VERSION = "ATIF-v1.7"
CLAUDE_FALLBACK_MODEL = "claude-opus-4-8"
CODEX_FALLBACK_MODEL = "gpt-5.5"
SUPPORTED_SOURCES = frozenset({"claude_code", "codex"})


class ExternalSubagentRef(TypedDict):
    """A separately stored child trajectory referenced from a parent ATIF."""

    trajectory_id: str
    session_id: str
    trajectory_path: str
    match_id: str


_SUBAGENT_ID_FIELDS = frozenset({
    "agentid",
    "agentids",
    "childthreadid",
    "receiverthreadids",
    "sessionid",
    "sessionids",
    "threadid",
    "threadids",
})


def _collect_strings(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        if value:
            found.add(value)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, found)


def _result_identifiers(value: Any) -> set[str]:
    """Return structured agent/session IDs found in one tool result."""
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if normalized in _SUBAGENT_ID_FIELDS:
                    _collect_strings(nested, found)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith(("{", "[")):
                try:
                    visit(json.loads(stripped))
                except json.JSONDecodeError:
                    pass

    visit(value)
    return found


def _result_mentions(value: Any, aliases: Iterable[str]) -> set[str]:
    """Return known aliases that appear as complete tokens in a result."""
    patterns = {
        alias: re.compile(
            rf"(?<![A-Za-z0-9_-]){re.escape(alias)}(?![A-Za-z0-9_-])"
        )
        for alias in aliases
    }
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            for alias, pattern in patterns.items():
                if alias not in found and pattern.search(item):
                    found.add(alias)

    visit(value)
    return found


def _is_spawn_tool(source_id: str, function_name: str) -> bool:
    normalized = re.sub(
        r"[^a-z0-9]+", "_", function_name.casefold()
    ).strip("_")
    if source_id == "claude_code":
        return normalized in {"agent", "task"}
    if source_id == "codex":
        return normalized.endswith("spawn_agent")
    return False


def _reference_aliases(source_id: str, session_id: str) -> set[str]:
    aliases = {session_id}
    if source_id == "claude_code" and session_id.startswith(("agent-", "agent_")):
        aliases.add(session_id[6:])
    return aliases


def _enrich_subagent_references(
    trajectory: Trajectory,
    source_id: str,
    subagent_refs: Iterable[ExternalSubagentRef],
) -> int:
    """Attach exact external child references to their spawning tool results."""
    from harbor.models.trajectories import SubagentTrajectoryRef

    aliases: dict[str, ExternalSubagentRef | None] = {}
    for reference in subagent_refs:
        for alias in _reference_aliases(source_id, reference["match_id"]):
            if alias not in aliases:
                aliases[alias] = reference
            elif aliases[alias] != reference:
                aliases[alias] = None
    if not aliases:
        return 0

    added = 0
    for step in trajectory.steps:
        spawn_calls = {
            call.tool_call_id
            for call in step.tool_calls or []
            if _is_spawn_tool(source_id, call.function_name)
        }
        if not spawn_calls or step.observation is None:
            continue
        for result in step.observation.results:
            if result.source_call_id not in spawn_calls:
                continue
            identifiers = _result_identifiers(result.content)
            identifiers.update(_result_identifiers(result.extra))
            identifiers.update(_result_mentions(result.content, aliases))
            identifiers.update(_result_mentions(result.extra, aliases))
            matched = {
                reference["trajectory_id"]: reference
                for identifier in identifiers
                if (reference := aliases.get(identifier)) is not None
            }
            if not matched:
                continue
            references = list(result.subagent_trajectory_ref or [])
            existing_ids = {reference.trajectory_id for reference in references}
            for trajectory_id, reference in sorted(matched.items()):
                if trajectory_id in existing_ids:
                    continue
                references.append(
                    SubagentTrajectoryRef(
                        trajectory_id=reference["trajectory_id"],
                        session_id=reference["session_id"],
                        trajectory_path=reference["trajectory_path"],
                    )
                )
                existing_ids.add(trajectory_id)
                added += 1
            result.subagent_trajectory_ref = references
    return added


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
    *,
    subagent_refs: Iterable[ExternalSubagentRef] = (),
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
    _enrich_subagent_references(trajectory, source_id, subagent_refs)
    return _serialize(trajectory), conversion_manifest(source_id, status="complete")


def derive_atif(
    source_id: str,
    raw: str,
    native_filename: str,
    session_id: str,
    parent_session_id: str | None = None,
    *,
    subagent_refs: Iterable[ExternalSubagentRef] = (),
) -> tuple[bytes | None, dict[str, Any]]:
    """Best-effort ATIF derivation that never drops the canonical transcript."""
    try:
        return convert_redacted_transcript(
            source_id,
            raw,
            native_filename,
            session_id,
            parent_session_id,
            subagent_refs=subagent_refs,
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
    "ExternalSubagentRef",
    "SUPPORTED_SOURCES",
    "convert_redacted_transcript",
    "derive_atif",
]
