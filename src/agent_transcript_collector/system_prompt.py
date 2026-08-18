"""Record the system prompt a session ran with, without bloating every archive.

Sources differ in what they record. Codex writes its full instructions into the
transcript's ``session_meta`` header, so nothing extra is needed. Claude Code
never writes its system prompt to the transcript at all; it can only be
observed on the API request, so ``tools/capture_system_prompt.py`` records one
alongside the session and this module reads what that left behind.

Identical prompts are common — every session on one CLI version and tool set
shares one — so the text is content-addressed: an archive carries the full text
only the first time a hash is seen, and afterwards refers to it by hash. What
was already sent is tracked per contributor in the state directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .paths import state_dir

SYSTEM_PROMPT_FILENAME = "system_prompt.txt"

INCLUDED = "included"      # full text is in this archive
UNCHANGED = "unchanged"    # same text was sent with an earlier archive
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


def _sent_hashes_path() -> Path:
    return state_dir() / "system-prompt-hashes.json"


def load_sent_hashes() -> dict:
    try:
        data = json.loads(_sent_hashes_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_sent_hash(digest: str, contributor: str) -> None:
    """Note that this text has been uploaded, so later archives can refer to it."""
    path = _sent_hashes_path()
    sent = load_sent_hashes()
    sent.setdefault(contributor, [])
    if digest not in sent[contributor]:
        sent[contributor].append(digest)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(sent, indent=2, sort_keys=True) + "\n")


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


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def describe(
    text: str | None,
    origin: str,
    *,
    contributor: str,
    already_sent: set[str],
) -> dict:
    """Build the manifest block for a session's system prompt."""
    if not text:
        return {"status": UNAVAILABLE, "origin": origin}
    digest = digest_of(text)
    status = UNCHANGED if digest in already_sent else INCLUDED
    record = {
        "status": status,
        "origin": origin,
        "sha256": digest,
        "chars": len(text),
    }
    if status == INCLUDED:
        record["artifact"] = SYSTEM_PROMPT_FILENAME
    return record


def trajectory_note(record: dict) -> str:
    """One line standing in for a prompt an archive refers to but does not carry."""
    return (
        "[system prompt not repeated in this archive: "
        f"sha256 {record.get('sha256', 'unknown')}, {record.get('chars', 0)} chars, "
        f"first sent with an earlier upload from this contributor]"
    )
