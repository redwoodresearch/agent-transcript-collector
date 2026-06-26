"""Transcript source registry.

Each source is one agent harness. `detect_all()` returns only the sources that
are actually present on this machine, ready to render in the UI. `find_session`
resolves a (source, group, session) selection back to the discovered Session,
so paths are never built from user-supplied strings.
"""

from __future__ import annotations

from .base import Group, Session, Source
from .claude_code import ClaudeCodeSource
from .codex import CodexSource
from .pi import PiSource

SOURCES: list[Source] = [ClaudeCodeSource(), CodexSource(), PiSource()]

_BY_ID = {s.id: s for s in SOURCES}


def get_source(source_id: str) -> Source | None:
    return _BY_ID.get(source_id)


def detect_all() -> list[dict]:
    """Discover every present source as template-ready dicts.

    Sources with no sessions are omitted entirely.
    """
    detected = []
    for source in SOURCES:
        groups = source.discover()
        if not groups:
            continue
        session_count = sum(g.session_count for g in groups)
        total_bytes = sum(g.total_size_bytes for g in groups)
        from .base import human_size
        detected.append({
            "id": source.id,
            "label": source.label,
            "session_count": session_count,
            "total_size_human": human_size(total_bytes),
            "groups": [
                {
                    "key": g.key,
                    "label": g.label,
                    "session_count": g.session_count,
                    "total_size_human": g.total_size_human,
                    "sessions": [s.as_dict() for s in g.sessions],
                }
                for g in groups
            ],
        })
    return detected


def find_session(source_id: str, group_key: str, session_id: str,
                 parent: str | None = None) -> Session | None:
    # Subagents share their parent's group, so id is unique only within
    # (source, group, parent) — match on parent too to avoid collisions.
    source = get_source(source_id)
    if source is None:
        return None
    parent = parent or None
    for group in source.discover():
        if group.key != group_key:
            continue
        for session in group.sessions:
            if session.id == session_id and (session.parent or None) == parent:
                return session
    return None


__all__ = [
    "SOURCES",
    "Group",
    "Session",
    "Source",
    "get_source",
    "detect_all",
    "find_session",
]
