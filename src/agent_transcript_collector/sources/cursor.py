"""Cursor Agent transcript source.

Layout: $CURSOR_HOME/projects/<encoded-project>/agent-transcripts/
          <conversation-id>/<conversation-id>.jsonl
        Legacy Cursor versions may also have flat *.txt transcripts.
        (default $CURSOR_HOME is ~/.cursor)

Format: Composer 2 JSONL entries with top-level `role` and
        `message.content` blocks. Cursor records user messages, assistant text,
        and tool-call inputs; tool outputs are intentionally not present in
        these files. The collector preserves the raw redacted transcript, while
        previews are best-effort.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .base import Group, Session, mtime, truncate


def _cursor_home() -> Path:
    override = os.environ.get("CURSOR_HOME")
    return Path(override) if override else Path.home() / ".cursor"


def _projects_dir() -> Path:
    return _cursor_home() / "projects"


def _user_data_dir() -> Path:
    override = os.environ.get("CURSOR_USER_DATA_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return Path(base) / "Cursor" / "User" if base else Path.home() / "AppData/Roaming/Cursor/User"
    if sys_platform := os.environ.get("XDG_CONFIG_HOME"):
        linux_default = Path(sys_platform) / "Cursor" / "User"
    else:
        linux_default = Path.home() / ".config" / "Cursor" / "User"
    mac_default = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    return mac_default if mac_default.exists() else linux_default


def _encode_project_path(path: str) -> str:
    return path.replace("\\", "/").strip("/").replace("/", "-")


def decode_project_name(encoded: str) -> str:
    """Decode Cursor's project directory name back into a likely path.

    Cursor derives project folders from absolute paths by replacing separators
    with dashes (for example, `Users-alice-src-app`). Some entries are synthetic
    ids such as `empty-window` or timestamps; leave those readable as-is.
    """
    if not encoded or encoded == "empty-window" or encoded.isdigit():
        return encoded or "(unknown project)"
    normalized = encoded.replace("\\", "-").strip("-")
    if not normalized:
        return "/"
    return "/" + normalized.replace("-", "/")


def _extract_paths(obj) -> list[str]:
    found = []
    if isinstance(obj, dict):
        for key in ("fsPath", "path"):
            val = obj.get(key)
            if isinstance(val, str) and val.startswith(("/", "\\")):
                found.append(val)
        for val in obj.values():
            found.extend(_extract_paths(val))
    elif isinstance(obj, list):
        for val in obj:
            found.extend(_extract_paths(val))
    return found


def _project_label_map() -> dict[str, str]:
    db = _user_data_dir() / "globalStorage" / "state.vscdb"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    labels: dict[str, str] = {}
    try:
        rows = conn.execute(
            "select value from ItemTable where key in "
            "('glass.localAgentProjects.v1', 'glass.cloudAgentProjects.v1')"
        )
        for (raw,) in rows:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str):
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for path in _extract_paths(data):
                labels.setdefault(_encode_project_path(path), path)
    except sqlite3.Error:
        return labels
    finally:
        conn.close()
    return labels


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name") or block.get("toolName") or "?"
                out.append(f"[Tool: {name}]")
        return "\n".join(part for part in out if part)
    return str(content)


def _jsonl_message(entry: dict) -> tuple[str, str] | None:
    role = entry.get("role")
    if role not in ("user", "assistant", "system"):
        return None
    message = entry.get("message")
    if isinstance(message, dict):
        text = _block_text(message.get("content", ""))
    else:
        text = _block_text(entry.get("content", ""))
    return role, text


class CursorSource:
    id = "cursor"
    label = "Cursor"
    source_format = "cursor-agent-transcript"

    def discover(self) -> list[Group]:
        projects_dir = _projects_dir()
        if not projects_dir.exists():
            return []

        label_by_key = _project_label_map()
        groups: list[Group] = []
        for project_dir in sorted(projects_dir.iterdir()):
            transcripts_dir = project_dir / "agent-transcripts"
            if not project_dir.is_dir() or not transcripts_dir.exists():
                continue
            label = label_by_key.get(project_dir.name) or decode_project_name(project_dir.name)
            sessions: list[Session] = []
            for f in self._transcript_files(transcripts_dir):
                first, count = self._summary(f)
                parent = self._parent_id(transcripts_dir, f)
                sessions.append(Session(
                    source=self.id,
                    id=self._session_id(transcripts_dir, f),
                    group_key=project_dir.name,
                    group_label=label,
                    path=f,
                    size_bytes=f.stat().st_size,
                    first_message=first,
                    message_count=count,
                    modified=mtime(f),
                    is_subagent=parent is not None,
                    parent=parent,
                ))
            if sessions:
                groups.append(Group(key=project_dir.name, label=label, sessions=sessions))
        return groups

    def _transcript_files(self, transcripts_dir: Path) -> list[Path]:
        files = []
        for f in transcripts_dir.rglob("*"):
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.suffix.lower() in (".jsonl", ".txt"):
                files.append(f)
        return sorted(files)

    def _session_id(self, transcripts_dir: Path, path: Path) -> str:
        rel = path.relative_to(transcripts_dir)
        if len(rel.parts) > 1 and rel.parts[0] != "subagents":
            return rel.parts[-2]
        return path.stem

    def _parent_id(self, transcripts_dir: Path, path: Path) -> str | None:
        rel = path.relative_to(transcripts_dir)
        parts = rel.parts
        if "subagents" not in parts:
            return None
        idx = parts.index("subagents")
        if idx == 0:
            return None
        return parts[idx - 1]

    def _summary(self, path: Path) -> tuple[str, int]:
        if path.suffix.lower() == ".jsonl":
            return self._summary_jsonl(path)
        return self._summary_text(path)

    def _summary_jsonl(self, path: Path) -> tuple[str, int]:
        first = ""
        count = 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(empty session)", 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            msg = _jsonl_message(entry)
            if msg is None:
                continue
            role, text = msg
            if role in ("user", "assistant"):
                count += 1
            if not first and role == "user" and text.strip():
                first = truncate(text)
        return first or "(empty session)", count

    def _summary_text(self, path: Path) -> tuple[str, int]:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(empty session)", 0
        messages = self._parse_text_messages(raw)
        first = next((m["text"] for m in messages if m["role"] == "user" and m["text"].strip()), "")
        return truncate(first) if first else "(empty session)", len(messages)

    def parse_messages(self, raw: str) -> list[dict]:
        messages = []
        parsed_any_json = False
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            parsed_any_json = True
            msg = _jsonl_message(entry)
            if msg is None:
                continue
            role, text = msg
            messages.append({"role": role, "text": text})
        if parsed_any_json:
            return messages
        return self._parse_text_messages(raw)

    def _parse_text_messages(self, raw: str) -> list[dict]:
        messages = []
        cur_role: str | None = None
        cur_lines: list[str] = []

        def flush() -> None:
            if cur_role and cur_lines:
                text = "\n".join(cur_lines).strip()
                if text:
                    messages.append({"role": cur_role, "text": text})

        for line in raw.splitlines():
            stripped = line.strip()
            lowered = stripped.rstrip(":").lower()
            if lowered in ("user", "human"):
                flush()
                cur_role, cur_lines = "user", []
            elif lowered in ("assistant", "cursor"):
                flush()
                cur_role, cur_lines = "assistant", []
            elif stripped.startswith(("User:", "Human:")):
                flush()
                cur_role, cur_lines = "user", [stripped.split(":", 1)[1].strip()]
            elif stripped.startswith(("Assistant:", "Cursor:")):
                flush()
                cur_role, cur_lines = "assistant", [stripped.split(":", 1)[1].strip()]
            elif cur_role:
                cur_lines.append(line)
        flush()

        if messages:
            return messages
        text = raw.strip()
        return [{"role": "assistant", "text": text}] if text else []
