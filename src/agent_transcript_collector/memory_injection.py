"""Record the memory a session was given, alongside the prompt it ran under.

Claude Code injects CLAUDE.md files and the user's auto-memory into the first
user message as a ``<system-reminder>`` block, and — like the system prompt —
does not keep it in the saved transcript. It is visible on the API request, so
the same watcher that reads request bodies for the system prompt takes this
from them too; no extra capture and no extra disk cost.

Every archive carries the memory it describes. The files it came from are
recorded separately so a reader can see what was in scope without opening the
text, and the sha256 is recorded so the corpus can be deduplicated later if
that turns out to be worth doing.
"""

from __future__ import annotations

import json
import re

from .system_prompt import INCLUDED, UNAVAILABLE, capture_path, digest_of

MEMORY_FILENAME = "injected_memory.txt"

# The reminder that carries memory announces itself; the others in the same
# message carry tool, skill and budget scaffolding, which is not memory.
_MEMORY_MARKER = "# claudeMd"
_REMINDER_OPEN = "<system-reminder>"
# "Contents of /path/to/CLAUDE.md (project instructions, checked into the codebase):"
_SOURCE_RE = re.compile(r"Contents of (?P<path>\S+?) \((?P<kind>[^)]*)\)", re.MULTILINE)


def memory_from_request(body: dict) -> str | None:
    """Pull the injected-memory reminders out of an API request body.

    Injection is not limited to the first message — memory can be re-injected
    later in a session, after an edit — so every message is scanned and each
    distinct block kept, in order.

    A block must be an actual reminder, not merely mention one: a conversation
    about memory injection quotes these markers, and matching on the marker
    alone would capture the discussion instead of the thing discussed.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    blocks: list[str] = []
    for message in messages:
        content = (message or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            if not text.lstrip().startswith(_REMINDER_OPEN):
                continue
            if _MEMORY_MARKER in text and text not in blocks:
                blocks.append(text)
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


def describe(text: str | None) -> dict:
    """Build the manifest block describing what memory a session was given."""
    if not text:
        return {"status": UNAVAILABLE}
    return {
        "status": INCLUDED,
        "sha256": digest_of(text),
        "chars": len(text),
        "sources": sources_in(text),
        "artifact": MEMORY_FILENAME,
    }

