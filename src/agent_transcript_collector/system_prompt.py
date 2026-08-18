"""Record the system prompt a session ran with, without bloating every archive.

Sources differ in what they record. Codex writes its full instructions into the
transcript's ``session_meta`` header, so nothing extra is needed. Claude Code
never writes its system prompt to the transcript at all; it can only be
observed on the API request, so ``tools/capture_system_prompt.py`` records one
alongside the session and this module reads what that left behind.

Every archive carries the full text of what it describes. Prompts do repeat
between sessions, but each one embeds per-session values — a prompt id, the
working directory — so identical copies are rarer than they look, and an
archive that refers to text stored in some other upload is only useful to a
reader who can find that upload. Deduplicating is better done later, over the
whole corpus, than guessed at one archive at a time. The recorded sha256 is
there to make that possible.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .paths import state_dir

SYSTEM_PROMPT_FILENAME = "system_prompt.txt"

INCLUDED = "included"      # full text is in this archive
IN_TRANSCRIPT = "in_transcript"  # the source recorded it; the transcript already has it
UNAVAILABLE = "unavailable"  # the source does not record it and none was captured


def captures_dir() -> Path:
    """Where the capture wrapper leaves prompts for the collector to pick up."""
    return state_dir() / "system-prompts"


def capture_path(source_id: str, session_id: str) -> Path:
    safe_session = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in session_id
    )
    return captures_dir() / source_id / f"{safe_session}.json"


def codex_system_prompt(raw: str) -> str | None:
    """Read the instructions Codex records in its session header."""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "session_meta":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        instructions = payload.get("base_instructions")
        if isinstance(instructions, dict):
            text = instructions.get("text")
            return text if isinstance(text, str) and text else None
        return instructions if isinstance(instructions, str) and instructions else None
    return None


def captured_system_prompt(source_id: str, session_id: str) -> str | None:
    """Read a prompt left by the capture wrapper for this session."""
    try:
        record = json.loads(capture_path(source_id, session_id).read_text())
    except (OSError, ValueError):
        return None
    text = record.get("text") if isinstance(record, dict) else None
    return text if isinstance(text, str) and text else None


def captured_variant_count(source_id: str, session_id: str) -> int:
    """How many substantively different prompts a session was seen using."""
    try:
        record = json.loads(capture_path(source_id, session_id).read_text())
    except (OSError, ValueError):
        return 0
    return len(record.get("prompt_variants") or [])


def resolve(source_id: str, raw: str, session_id: str) -> tuple[str | None, str]:
    """Return the session's system prompt text and where it came from."""
    if source_id == "codex":
        text = codex_system_prompt(raw)
        if text:
            return text, "session_meta"
    text = captured_system_prompt(source_id, session_id)
    if text:
        return text, "captured"
    return None, "unavailable"


# Every request carries a fresh id in this header, so two prompts that are
# otherwise identical never hash the same. Comparisons ignore it.
_VOLATILE_PREFIX = "x-anthropic-billing-header:"


def stable_text(text: str) -> str:
    """The prompt with its per-request header removed, for comparing turns."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(_VOLATILE_PREFIX)
    )


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def describe(text: str | None, origin: str, variants: int = 0) -> dict:
    """Build the manifest block for a session's system prompt."""
    if not text:
        return {"status": UNAVAILABLE, "origin": origin}
    digest = digest_of(text)
    if origin == "session_meta":
        # Codex writes its instructions into the transcript, which this archive
        # already carries, and Harbor renders them as steps. Record the hash so
        # the prompt is still identifiable, but do not store a second copy.
        return {
            "status": IN_TRANSCRIPT,
            "origin": origin,
            "sha256": digest,
            "chars": len(text),
        }
    record = {
        "status": INCLUDED,
        "origin": origin,
        "sha256": digest,
        "chars": len(text),
        "artifact": SYSTEM_PROMPT_FILENAME,
    }
    if variants > 1:
        # The stored text is one of several the session ran under.
        record["variants_seen"] = variants
    return record

