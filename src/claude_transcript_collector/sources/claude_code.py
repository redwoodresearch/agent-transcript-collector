"""Claude Code transcript source.

Layout: $CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-uuid>.jsonl
        (default $CLAUDE_CONFIG_DIR is ~/.claude)
Format: JSONL; entries have type "user"/"assistant" and a `message.content`
        that is either a string or a list of content blocks.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import Group, Session, iter_jsonl, mtime, truncate


def _config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _projects_dir() -> Path:
    return _config_dir() / "projects"


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

    def discover(self) -> list[Group]:
        projects_dir = _projects_dir()
        if not projects_dir.exists():
            return []

        groups: list[Group] = []
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            sessions: list[Session] = []
            for f in sorted(project_dir.glob("*.jsonl")):
                first, count = self._summary(f)
                sessions.append(Session(
                    source=self.id,
                    id=f.stem,
                    group_key=project_dir.name,
                    group_label=decode_project_name(project_dir.name),
                    path=f,
                    size_bytes=f.stat().st_size,
                    first_message=first,
                    message_count=count,
                    modified=mtime(f),
                ))
            if sessions:
                groups.append(Group(
                    key=project_dir.name,
                    label=decode_project_name(project_dir.name),
                    sessions=sessions,
                ))
        return groups

    def _summary(self, path: Path) -> tuple[str, int]:
        first = ""
        count = 0
        for entry in iter_jsonl(path):
            etype = entry.get("type")
            if etype in ("user", "assistant"):
                count += 1
            if not first and etype == "user":
                text = _block_text(entry.get("message", {}).get("content", "")).strip()
                if text:
                    first = truncate(text)
        return first or "(empty session)", count

    def parse_messages(self, raw: str) -> list[dict]:
        messages = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                import json
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") in ("user", "assistant"):
                text = _block_text(entry.get("message", {}).get("content", ""))
                messages.append({"role": entry["type"], "text": text})
        return messages
