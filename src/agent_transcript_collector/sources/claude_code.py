"""Claude Code transcript source.

Layout: $CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-uuid>.jsonl
        (default $CLAUDE_CONFIG_DIR is ~/.claude)
Format: JSONL; entries have type "user"/"assistant" and a `message.content`
        that is either a string or a list of content blocks.

Oversized tool results and background-task output are written outside the
transcript, which keeps only a pointer to them; see `sidecars` below.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..sidecars import SidecarBuilder, SidecarSet
from .base import (
    Session,
    canonical_project_directory,
    decode_existing_project_path,
    iter_jsonl,
    mtime,
    project_identity,
    truncate,
)

_PATH_DELIMITERS = frozenset(" \t\r\n\"'\\<>,)]")


def _sidecar_pointers(raw_text: str, marker: str, suffix: str = "") -> list[str]:
    """Extract path tokens around a literal marker in one linear pass.

    The previous leading-wildcard regex backtracked across every slash in a
    large transcript when no pointer existed, turning a cheap check into
    minutes of CPU work.
    """
    pointers = []
    position = 0
    while True:
        marker_at = raw_text.find(marker, position)
        if marker_at < 0:
            return pointers
        start = marker_at
        while start > 0 and raw_text[start - 1] not in _PATH_DELIMITERS:
            start -= 1
        end = marker_at + len(marker)
        while (
            end < len(raw_text)
            and raw_text[end] not in _PATH_DELIMITERS
            and raw_text[end] != "/"
        ):
            end += 1
        pointer = raw_text[start:end]
        if pointer.startswith("/") and (not suffix or pointer.endswith(suffix)):
            pointers.append(pointer)
        position = end if end > marker_at else marker_at + len(marker)


def _config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _projects_dir() -> Path:
    return _config_dir() / "projects"


def _task_output_roots() -> list[Path]:
    """Per-user temp folders holding background-task output, e.g. /tmp/claude-501/."""
    bases = {Path(tempfile.gettempdir()), Path("/tmp")}
    return [
        directory
        for base in bases
        if base.is_dir()
        for directory in base.glob("claude-*")
        if directory.is_dir()
    ]


def decode_project_name(encoded: str) -> str:
    """Decode a project folder name back into a path.

    e.g. '-Users-alice-Git-foo' -> '/Users/alice/Git/foo'
    """
    if not encoded:
        return encoded
    parts = encoded.split("-")
    if parts and parts[0] == "":
        parts = parts[1:]
    return "/" + "/".join(parts)


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append(block.get("text", ""))
            elif btype == "tool_use":
                out.append(f"[Tool: {block.get('name', '?')}]")
            elif btype == "tool_result":
                out.append("[Tool Result]")
        return "\n".join(out)
    return str(content)


class ClaudeCodeSource:
    id = "claude_code"
    label = "Claude Code"
    source_format = "claude-jsonl"

    def discover(self) -> list[Session]:
        projects_dir = _projects_dir()
        if not projects_dir.exists():
            return []

        sessions = []
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            files: list[tuple[Path, str | None]] = [
                (f, None) for f in sorted(project_dir.glob("*.jsonl"))
            ]
            files.extend(
                (f, f.parent.parent.name)
                for f in sorted(project_dir.glob("*/subagents/*.jsonl"))
            )
            records = [
                (f, parent, *self._summary(f))
                for f, parent in files
            ]
            container_cwd = next((cwd for _, _, cwd, _, _ in records if cwd), None)
            for f, parent, cwd, first, count in records:
                fallback = decode_project_name(project_dir.name)
                recovered = (
                    decode_existing_project_path(project_dir.name)
                    if cwd is None and container_cwd is None
                    else None
                )
                observed_cwd = cwd or container_cwd or recovered or fallback
                _key, label = project_identity(observed_cwd)
                directory = (
                    canonical_project_directory(observed_cwd)
                    if cwd is not None or container_cwd is not None or recovered is not None
                    else None
                )
                sessions.append(Session(
                    source=self.id, id=f.stem,
                    project_id="", project_label=label,
                    project_directory=directory,
                    path=f, size_bytes=f.stat().st_size,
                    first_message=first, message_count=count, modified=mtime(f),
                    is_subagent=parent is not None, parent=parent,
                ))
        return sessions

    def _summary(self, path: Path) -> tuple[str | None, str, int]:
        cwd = None
        first = ""
        count = 0
        for entry in iter_jsonl(path):
            if cwd is None and isinstance(entry.get("cwd"), str):
                cwd = entry["cwd"]
            etype = entry.get("type")
            if etype in ("user", "assistant"):
                count += 1
            if not first and etype == "user":
                text = _block_text(entry.get("message", {}).get("content", "")).strip()
                if text:
                    first = truncate(text)
        return cwd, first or "(empty session)", count

    def sidecars(self, session: Session, raw_text: str) -> SidecarSet:
        """Resolve the oversized tool results and task output this session saw.

        Pointers are followed rather than the session folder simply being
        listed, because a resumed session inherits the earlier session's
        folder and keeps pointing back at it.
        """
        builder = SidecarBuilder(roots=[_projects_dir(), *_task_output_roots()])
        for pointer in _sidecar_pointers(raw_text, "/tool-results/"):
            builder.add(pointer, "tool-results")
        for pointer in _sidecar_pointers(raw_text, "/tasks/", ".output"):
            builder.add(pointer, "task-outputs")
        if not session.is_subagent:
            # A session's own folder also holds output its subagents asked for,
            # which the parent transcript never names.
            builder.add_directory(
                Path(session.path).with_suffix("") / "tool-results", "tool-results"
            )
        return builder.build()

    def parse_messages(self, raw: str) -> list[dict]:
        messages = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") in ("user", "assistant"):
                text = _block_text(entry.get("message", {}).get("content", ""))
                messages.append({"role": entry["type"], "text": text})
        return messages
