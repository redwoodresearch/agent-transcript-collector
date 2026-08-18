"""Record the memory a session was given, alongside the prompt it ran under.

Claude Code injects CLAUDE.md files and the user's auto-memory into the first
user message as a ``<system-reminder>`` block, and — like the system prompt —
does not keep it in the saved transcript. It is visible on the API request, so
the same watcher that reads request bodies for the system prompt takes this
from them too; no extra capture and no extra disk cost.

Memory changes as work happens, so unlike a system prompt its hash rarely
repeats. The text is still content-addressed, and the files it came from are
recorded separately so a reader can see what was in scope without opening it.
"""

from __future__ import annotations

import json
import re

from .system_prompt import (
    INCLUDED,
    UNAVAILABLE,
    UNCHANGED,
    capture_path,
    digest_of,
)

MEMORY_FILENAME = "injected_memory.txt"

# The reminder that carries memory announces itself; the others in the same
# message carry tool, skill and budget scaffolding, which is not memory.
_MEMORY_MARKER = "# claudeMd"
# "Contents of /path/to/CLAUDE.md (project instructions, checked into the codebase):"
_SOURCE_RE = re.compile(r"Contents of (?P<path>\S+?) \((?P<kind>[^)]*)\)", re.MULTILINE)


def memory_from_request(body: dict) -> str | None:
    """Pull the injected-memory reminder out of an API request body."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    content = (messages[0] or {}).get("content")
    if not isinstance(content, list):
        return None
    blocks = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
        and _MEMORY_MARKER in block.get("text", "")
    ]
    return "\n\n".join(blocks) if blocks else None


def captured_memory(source_id: str, session_id: str) -> str | None:
    try:
        record = json.loads(capture_path(source_id, session_id).read_text())
    except (OSError, ValueError):
        return None
    text = record.get("memory") if isinstance(record, dict) else None
    return text if isinstance(text, str) and text else None


def sources_in(text: str) -> list[dict]:
    """List the files an injected block says it is quoting."""
    seen: dict[str, dict] = {}
    for match in _SOURCE_RE.finditer(text):
        path = match.group("path").rstrip(":")
        seen.setdefault(path, {"path": path, "kind": match.group("kind").strip()})
    return list(seen.values())


def describe(text: str | None, *, already_sent: set[str]) -> dict:
    """Build the manifest block describing what memory a session was given."""
    if not text:
        return {"status": UNAVAILABLE}
    digest = digest_of(text)
    record = {
        "status": UNCHANGED if digest in already_sent else INCLUDED,
        "sha256": digest,
        "chars": len(text),
        "sources": sources_in(text),
    }
    if record["status"] == INCLUDED:
        record["artifact"] = MEMORY_FILENAME
    return record


def trajectory_note(record: dict) -> str:
    return (
        "[injected memory not repeated in this archive: "
        f"sha256 {record.get('sha256', 'unknown')}, {record.get('chars', 0)} chars, "
        "first sent with an earlier upload from this contributor]"
    )
