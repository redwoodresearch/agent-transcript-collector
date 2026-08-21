"""Classify how a session was driven: an interactive agent session, or not.

Claude Code stamps each prompt-initiating event with `origin.kind` ("human",
"task-notification", ...), `promptSource` ("typed" | "queued" | "sdk" |
"system") and `entrypoint` ("cli" | "sdk-cli"), so its label reflects where
each message came from. Codex instead states how the process was launched once,
in its `session_meta` header — "codex-tui"/"cli" for a terminal session,
"codex_exec"/"exec" for `codex exec` — which is coarser: it says nothing about
who supplied the prompt text.

Neither direction is proof of who was present. A script driving an interactive
session by sending keystrokes looks exactly like a person, so "human" is
evidence rather than certainty; and "programmatic" only means the run was not
interactive — a developer typing `claude -p` at their own shell lands there
too. Treat the label as a description of how the agent was invoked.
"""

from __future__ import annotations

import json
from pathlib import Path

HUMAN = "human"
PROGRAMMATIC = "programmatic"
UNKNOWN = "unknown"

_HUMAN_PROMPT_SOURCES = {"typed", "queued"}
_PROGRAMMATIC_PROMPT_SOURCES = {"sdk"}
# One entrypoint per Claude Agent SDK target. Only the headless CLI stamps
# promptSource "sdk" on its events, so for the Python and TypeScript SDKs the
# entrypoint is the only thing separating an SDK run from an interactive one -
# without them those sessions read as unknown rather than programmatic.
_PROGRAMMATIC_ENTRYPOINTS = {"sdk-cli", "sdk-py", "sdk-ts"}

# Codex records how a session started once, in its session_meta header, rather
# than per prompt: an interactive terminal reports originator "codex-tui" with
# source "cli", while `codex exec` reports "codex_exec" with source "exec".
_HUMAN_CODEX_ORIGINATORS = {"codex-tui", "codex_tui", "codex-vscode", "codex_vscode"}
_PROGRAMMATIC_CODEX_SOURCES = {"exec"}
_PROGRAMMATIC_CODEX_ORIGINATOR_MARKERS = ("exec", "mcp", "sdk", "api")


def _codex_launch_kind(event: dict) -> str | None:
    """Read Codex's session header, which states how the session was started."""
    if event.get("type") != "session_meta":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    originator = str(payload.get("originator") or "").casefold()
    source = str(payload.get("source") or "").casefold()
    if source in _PROGRAMMATIC_CODEX_SOURCES or any(
        marker in originator for marker in _PROGRAMMATIC_CODEX_ORIGINATOR_MARKERS
    ):
        return PROGRAMMATIC
    if originator in _HUMAN_CODEX_ORIGINATORS:
        return HUMAN
    return None


def _event_launch_kind(event: dict) -> str | None:
    codex = _codex_launch_kind(event)
    if codex is not None:
        return codex
    origin = event.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == HUMAN:
        return HUMAN
    prompt_source = event.get("promptSource")
    if prompt_source in _HUMAN_PROMPT_SOURCES:
        return HUMAN
    if prompt_source in _PROGRAMMATIC_PROMPT_SOURCES:
        return PROGRAMMATIC
    if event.get("entrypoint") in _PROGRAMMATIC_ENTRYPOINTS:
        return PROGRAMMATIC
    return None


def find_parent_transcript(path: Path, parent_id: str) -> Path | None:
    """Locate a session's parent transcript near it on disk.

    Layouts differ by source — Claude Code files a subagent under
    ``<project>/<parent>/subagents/<child>.jsonl`` while the parent sits at
    ``<project>/<parent>.jsonl`` — so rather than assume one shape, look for a
    file named after the parent in the nearby directories and take the first
    that exists. An id containing a path separator is not a filename and is
    rejected outright.
    """
    if not parent_id or "\0" in parent_id or "/" in parent_id or "\\" in parent_id:
        return None
    filename = f"{parent_id}{path.suffix}"
    seen = []
    for ancestor in (path.parent, *list(path.parents)[1:3]):
        if ancestor in seen:
            continue
        seen.append(ancestor)
        candidate = ancestor / filename
        try:
            if candidate.is_file():
                return candidate
            # Codex names a session file rollout-<timestamp>-<id>.jsonl, so the
            # id is a suffix of the name rather than the whole of it.
            match = next(ancestor.glob(f"*-{parent_id}{path.suffix}"), None)
            if match is not None and match.is_file():
                return match
        except OSError:
            continue
    return None


def session_launch_kind(
    transcript: str,
    path: Path | None = None,
    parent_id: str | None = None,
) -> str:
    """Classify a session, letting a subagent inherit its parent's label.

    A subagent has no prompts of its own — it is spawned by a tool call — so on
    its own it always reads "unknown". What matters for analysis is whether the
    run it belongs to was human-driven, so the parent's label is inherited when
    the parent transcript can be found.
    """
    kind = launch_kind(transcript)
    if kind != UNKNOWN or path is None or not parent_id:
        return kind
    parent_path = find_parent_transcript(path, parent_id)
    if parent_path is None:
        return UNKNOWN
    try:
        return launch_kind(parent_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return UNKNOWN


def launch_kind(transcript: str) -> str:
    """Return "human" when any prompt in the transcript came from a person."""
    saw_programmatic = False
    for line in transcript.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        verdict = _event_launch_kind(event)
        if verdict == HUMAN:
            return HUMAN
        if verdict == PROGRAMMATIC:
            saw_programmatic = True
    return PROGRAMMATIC if saw_programmatic else UNKNOWN
