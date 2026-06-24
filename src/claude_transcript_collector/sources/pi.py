"""Pi coding-agent transcript source (earendil-works/pi).

Layout: <session-dir>/--<encoded-cwd>--/<timestamp>_<sessionId>.jsonl
  session-dir resolution (highest priority first):
    1. $PI_CODING_AGENT_SESSION_DIR
    2. $PI_CODING_AGENT_DIR/sessions
    3. ~/.pi/agent/sessions
  Plus a flat fallback glob of <agent-dir>/*.jsonl to catch transcripts written
  by older buggy versions (earendil-works/pi#320).
Format: JSONL v3. Line 1 is a session header {"type":"session", "cwd":...,
        "version":3, "id":...}. Remaining lines are entries; message entries are
        {"type":"message", "message":{"role":..., "content": str|blocks}}.
        Roles include user/assistant/toolResult/bashExecution/custom; content is
        either a string or a list of blocks (text/thinking/toolCall/image).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import Group, Session, mtime, truncate

_CONTENT_ROLES = ("user", "assistant")


def _agent_dir() -> Path:
    override = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(override) if override else Path.home() / ".pi" / "agent"


def _session_dir() -> Path:
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return Path(override)
    return _agent_dir() / "sessions"


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("\\", "/").lstrip("/").replace("/", "-") or "_root"


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    parts.append(block)
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                parts.append("[thinking]")
            elif btype == "toolCall":
                parts.append(f"[Tool: {block.get('name', '?')}]")
            elif btype == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return str(content)


def _read_objects(path: Path) -> list[dict]:
    objs = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    objs.append(obj)
    except OSError:
        return []
    return objs


def _is_pi_transcript(objs: list[dict]) -> bool:
    return bool(objs) and objs[0].get("type") == "session"


class PiSource:
    id = "pi"
    label = "Pi"
    source_format = "pi-session-jsonl-v3"

    def _candidate_files(self) -> list[Path]:
        files: list[Path] = []
        session_dir = _session_dir()
        if session_dir.exists():
            files.extend(session_dir.glob("--*--/*.jsonl"))
        # Flat fallback for older buggy versions that wrote to the agent dir.
        agent_dir = _agent_dir()
        if agent_dir.exists():
            files.extend(agent_dir.glob("*.jsonl"))
        return sorted(set(files))

    def discover(self) -> list[Group]:
        by_group: dict[str, Group] = {}
        for f in self._candidate_files():
            objs = _read_objects(f)
            if not _is_pi_transcript(objs):
                continue
            header = objs[0]
            cwd = header.get("cwd") or ""
            key = _encode_cwd(cwd) if cwd else "_ungrouped"
            label = cwd or "(unknown working dir)"
            sid = header.get("id") or f.stem.split("_", 1)[-1]
            first, count = self._summary(objs)

            group = by_group.get(key)
            if group is None:
                group = by_group[key] = Group(key=key, label=label, sessions=[])
            group.sessions.append(Session(
                source=self.id,
                id=sid,
                group_key=key,
                group_label=label,
                path=f,
                size_bytes=f.stat().st_size,
                first_message=first,
                message_count=count,
                modified=mtime(f),
            ))
        return list(by_group.values())

    def _summary(self, objs: list[dict]) -> tuple[str, int]:
        first = ""
        count = 0
        for obj in objs:
            if obj.get("type") != "message":
                continue
            msg = obj.get("message", {})
            role = msg.get("role")
            if role in _CONTENT_ROLES:
                count += 1
            if not first and role == "user":
                text = _block_text(msg.get("content", "")).strip()
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
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "message":
                continue
            msg = obj.get("message", {})
            role = msg.get("role", "user")
            messages.append({"role": role, "text": _block_text(msg.get("content", ""))})
        return messages
