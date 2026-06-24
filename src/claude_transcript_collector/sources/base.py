"""Base abstractions shared by all transcript sources.

A "source" is one agent harness (Claude Code, Codex, Pi, ...). Each source
knows where that harness stores transcripts on disk, how to discover and group
them, and how to parse a transcript into messages for preview. Everything
downstream (redaction, zipping, upload, the UI) is source-agnostic and works in
terms of the normalized types defined here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, skipping blank/garbage lines."""
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
                    yield obj
    except OSError:
        return


def mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def truncate(text: str, max_length: int = 200) -> str:
    text = text.strip()
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


@dataclass
class Session:
    source: str          # source id, e.g. "claude_code"
    id: str              # session id, unique within (source, group)
    group_key: str       # stable grouping key (used in archive paths)
    group_label: str     # human-readable group label (usually a cwd)
    path: Path           # absolute path to the transcript file on disk
    size_bytes: int
    first_message: str
    message_count: int
    modified: datetime | None = None

    @property
    def size_human(self) -> str:
        return human_size(self.size_bytes)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "group_key": self.group_key,
            "first_message": self.first_message,
            "message_count": self.message_count,
            "size_bytes": self.size_bytes,
            "size_human": self.size_human,
            "modified": self.modified,
        }


@dataclass
class Group:
    key: str
    label: str
    sessions: list[Session]

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def total_size_bytes(self) -> int:
        return sum(s.size_bytes for s in self.sessions)

    @property
    def total_size_human(self) -> str:
        return human_size(self.total_size_bytes)


@runtime_checkable
class Source(Protocol):
    id: str                # stable slug, used in S3 prefixes and URLs
    label: str             # display name
    source_format: str     # format tag recorded in the manifest

    def discover(self) -> list[Group]:
        """Return groups of sessions found on disk (empty if not installed)."""
        ...

    def parse_messages(self, raw: str) -> list[dict]:
        """Parse raw (possibly redacted) transcript text into [{role, text}]."""
        ...
