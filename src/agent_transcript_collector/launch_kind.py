"""Classify how a session was driven: an interactive agent session, or not.

Claude Code stamps each prompt-initiating event with `origin.kind` ("human",
"task-notification", ...), `promptSource` ("typed" | "queued" | "sdk" |
"system") and `entrypoint` ("cli" | one of the "sdk-*" Agent SDK targets), so
its label reflects where each message came from. Codex instead states how the
process was launched once, in its `session_meta` header — "codex-tui"/"cli" for
a terminal session, "codex_exec"/"exec" for `codex exec` — which is coarser: it
says nothing about who supplied the prompt text.

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
#
# `source` is the field Codex defines (the bare SessionSource strings listed in
# sources/codex.py), so both sets below are keyed on it first. The originator is
# a free-form host string — new ones appear without warning, and a session that
# names itself something we have never seen should still be classified by the
# source it reports rather than falling through to unknown.
_HUMAN_CODEX_SOURCES = {"cli", "vscode"}
_PROGRAMMATIC_CODEX_SOURCES = {"exec", "mcp"}
_HUMAN_CODEX_ORIGINATORS = {"codex-tui", "codex_tui", "codex-vscode", "codex_vscode"}
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
    if source in _HUMAN_CODEX_SOURCES or originator in _HUMAN_CODEX_ORIGINATORS:
        return HUMAN
    return None


def _event_launch_kind(event: dict) -> str | None:
    codex = _codex_launch_kind(event)
    if codex is not None:
        return codex
    # These fields answer different questions and the order matters. origin.kind
    # separates a real prompt from a synthetic one (task-notification); it does
    # not say how the prompt was submitted. promptSource and entrypoint do. A
    # developer who types `claude -p` is a person submitting over the SDK, and
    # the label we want for that is programmatic, so the submission channel is
    # read first. Today no event carries both markers, and this ordering is what
    # keeps that from being something the classification silently relies on.
    prompt_source = event.get("promptSource")
    if prompt_source in _PROGRAMMATIC_PROMPT_SOURCES:
        return PROGRAMMATIC
    if event.get("entrypoint") in _PROGRAMMATIC_ENTRYPOINTS:
        return PROGRAMMATIC
    origin = event.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == HUMAN:
        return HUMAN
    if prompt_source in _HUMAN_PROMPT_SOURCES:
        return HUMAN
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


def inherited_launch_kind(own: str, parent: str | None) -> str:
    """Combine a subagent's own reading with its parent's.

    The parent is the thing that was actually launched, so it decides whenever
    it says anything; the child's own reading is the fallback for a parent that
    is missing, unreadable, or itself unknown.
    """
    return parent if parent and parent != UNKNOWN else own


def session_launch_kind(
    transcript: str,
    path: Path | None = None,
    parent_id: str | None = None,
) -> str:
    """Classify a session, letting a subagent take its parent's label.

    A subagent is spawned by a tool call rather than by a prompt, so its own
    events describe the process it shares with its parent rather than how the
    run was started. The parent is the thing that was actually launched, so its
    label wins whenever it can be read, and the child's own reading is only the
    fallback. Deciding by the child first would make the answer depend on which
    fields a version of the agent happens to stamp on subagent events.
    """
    kind = launch_kind(transcript)
    if path is None or not parent_id:
        return kind
    parent_path = find_parent_transcript(path, parent_id)
    if parent_path is None:
        return kind
    try:
        parent = parent_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return kind
    return inherited_launch_kind(kind, launch_kind(parent))


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
