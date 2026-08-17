"""Classify who drove a session: a person at a keyboard, or a program.

Claude Code stamps each prompt-initiating event with `origin.kind` ("human",
"task-notification", ...), `promptSource` ("typed" | "queued" | "sdk" |
"system") and `entrypoint` ("cli" | "sdk-cli"). Print/SDK mode (`claude -p`,
the Agent SDK) is reliably marked, so an absence of human markers alongside SDK
markers means the run was launched programmatically.

The label is evidence, not proof: a script that drives an *interactive* session
by sending keystrokes looks exactly like a person to the CLI, so "human" can be
produced by automation. Treat "programmatic" as the trustworthy direction.
"""

from __future__ import annotations

import json

HUMAN = "human"
PROGRAMMATIC = "programmatic"
UNKNOWN = "unknown"

_HUMAN_PROMPT_SOURCES = {"typed", "queued"}
_PROGRAMMATIC_PROMPT_SOURCES = {"sdk"}
_PROGRAMMATIC_ENTRYPOINTS = {"sdk-cli"}


def _event_launch_kind(event: dict) -> str | None:
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
