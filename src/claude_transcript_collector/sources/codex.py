"""Codex (OpenAI Codex CLI) transcript source.

Layout: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
        (default $CODEX_HOME is ~/.codex)
Format: JSONL "rollout" items. The exact item schema varies by Codex version,
        so parsing here is intentionally tolerant: it pulls a (role, text) out
        of whatever shape it can and ignores the rest. The canonical artifact we
        collect is the raw redacted JSONL, so preview/metadata being best-effort
        does not affect what gets stored. Validate against a real rollout before
        relying on the previews.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import Group, Session, iter_jsonl, mtime, truncate

_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I
)


def _sessions_dir() -> Path:
    override = os.environ.get("CODEX_HOME")
    base = Path(override) if override else Path.home() / ".codex"
    return base / "sessions"


def _session_id(path: Path) -> str:
    m = _UUID_RE.search(path.stem)
    return m.group(1) if m else path.stem


def _find_cwd(obj: dict) -> str | None:
    """Best-effort: find a working-directory field in a rollout object."""
    if not isinstance(obj, dict):
        return None
    for key in ("cwd", "cwd_path", "working_directory"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    for nested in ("payload", "session", "meta", "session_meta"):
        sub = obj.get(nested)
        if isinstance(sub, dict):
            found = _find_cwd(sub)
            if found:
                return found
    return None


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("\\", "/").lstrip("/").replace("/", "-") or "_root"


def _extract_message(obj: dict) -> tuple[str, str] | None:
    """Pull (role, text) from a rollout item if it looks like a message."""
    node = obj
    if "role" not in node and isinstance(node.get("payload"), dict):
        node = node["payload"]
    role = node.get("role")
    if role not in ("user", "assistant", "system"):
        return None
    content = node.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("input_text", "output_text", "text"):
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    else:
        text = str(content)
    return role, text


def _payload(obj: dict) -> dict:
    p = obj.get("payload")
    return p if isinstance(p, dict) else obj


def _classify_source(source) -> str:
    """Classify a rollout's session_meta `source` (Codex `SessionSource`).

    Returns:
      - 'top'  : interactive/normal session — keep, unmarked.
      - 'task' : a genuine spawned subagent — keep, mark is_subagent.
      - 'drop' : automated scaffolding (review/compact/memory_consolidation,
                 or an internal session) — exclude.

    Schema (externally tagged) from openai/codex
    `codex-rs/protocol/src/protocol.rs` (SessionSource / SubAgentSource):
      "cli"|"vscode"|"exec"|"mcp"|"unknown"     -> top-level (bare strings)
      {"custom": "..."}                          -> top-level
      {"internal": ...}                          -> scaffolding
      {"subagent": "review"|"compact"|"memory_consolidation"}  -> scaffolding
      {"subagent": {"thread_spawn": {...}}}      -> genuine task subagent
      {"subagent": {"other": "..."}}             -> catch-all subagent
    """
    if not isinstance(source, dict):
        return "top"                       # cli / vscode / exec / mcp / unknown
    if "custom" in source:
        return "top"
    if "internal" in source:
        return "drop"                      # internal scaffolding (memory_consolidation)
    if "subagent" in source:
        sub = source["subagent"]
        if isinstance(sub, str):
            return "drop"                  # review / compact / memory_consolidation
        return "task"                      # thread_spawn or other -> real subagent
    return "top"                           # unknown shape: keep rather than drop


def _parent_thread_id(first_obj: dict, source) -> str | None:
    """Recover a subagent's parent id: thread_spawn.parent_thread_id, else the
    top-level session_meta.parent_thread_id."""
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict):
            ts = sub.get("thread_spawn")
            if isinstance(ts, dict) and isinstance(ts.get("parent_thread_id"), str):
                return ts["parent_thread_id"]
    pid = _payload(first_obj).get("parent_thread_id")
    return pid if isinstance(pid, str) else None


class CodexSource:
    id = "codex"
    label = "Codex"
    source_format = "codex-rollout-jsonl"

    def discover(self) -> list[Group]:
        sessions_dir = _sessions_dir()
        if not sessions_dir.exists():
            return []

        by_group: dict[str, Group] = {}
        for f in sorted(sessions_dir.rglob("rollout-*.jsonl")):
            first_obj = next(iter_jsonl(f), None)
            source = _payload(first_obj).get("source") if first_obj else None
            kind = _classify_source(source)
            if kind == "drop":
                continue  # review/compact/memory_consolidation/internal scaffolding
            cwd, first, count = self._summary(f)
            key = _encode_cwd(cwd) if cwd else "_ungrouped"
            label = cwd or "(unknown working dir)"
            group = by_group.get(key)
            if group is None:
                group = by_group[key] = Group(key=key, label=label, sessions=[])
            group.sessions.append(Session(
                source=self.id,
                id=_session_id(f),
                group_key=key,
                group_label=label,
                path=f,
                size_bytes=f.stat().st_size,
                is_subagent=(kind == "task"),
                parent=_parent_thread_id(first_obj, source) if kind == "task" else None,
                first_message=first,
                message_count=count,
                modified=mtime(f),
            ))
        return list(by_group.values())

    def _summary(self, path: Path) -> tuple[str | None, str, int]:
        cwd = None
        first = ""
        count = 0
        for obj in iter_jsonl(path):
            if cwd is None:
                cwd = _find_cwd(obj)
            msg = _extract_message(obj)
            if msg is None:
                continue
            role, text = msg
            if role in ("user", "assistant"):
                count += 1
            if not first and role == "user" and text.strip():
                first = truncate(text)
        return cwd, (first or "(empty session)"), count

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
            if not isinstance(obj, dict):
                continue
            msg = _extract_message(obj)
            if msg is None:
                continue
            role, text = msg
            messages.append({"role": role, "text": text})
        return messages
