"""Transcript source registry.

Each source is one agent harness. `detect_all()` returns only the sources that
are actually present on this machine, ready to render in the UI. `find_session`
resolves a (source, group, session) selection back to the discovered Session,
so paths are never built from user-supplied strings.
"""

from __future__ import annotations

from .base import Group, Session, Source, human_size
from .claude_code import ClaudeCodeSource
from .codex import CodexSource
from .cursor import CursorSource
from .pi import PiSource

SOURCES: list[Source] = [ClaudeCodeSource(), CodexSource(), CursorSource(), PiSource()]

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


def projects_from_groups(discovered: list[tuple[Source, Group]]) -> list[dict]:
    """Build the local UI's project tree from an existing discovery snapshot."""

    def modified_at(session: Session) -> float:
        return session.modified.timestamp() if session.modified else 0

    directories_by_label: dict[str, set[str]] = {}
    for _, group in discovered:
        if group.directory:
            directories_by_label.setdefault(group.label, set()).add(group.directory)

    projects_by_identity: dict[str, dict] = {}
    for source, group in discovered:
        directory = group.directory
        candidates = directories_by_label.get(group.label, set())
        if directory is None and len(candidates) == 1:
            directory = next(iter(candidates))
        identity = (
            f"directory:{directory}" if directory else f"unresolved:{group.label}"
        )
        project = projects_by_identity.get(identity)
        if project is None:
            project = projects_by_identity[identity] = {
                "key": f"project-{len(projects_by_identity)}",
                "label": group.label,
                "directory": directory,
                "session_count": 0,
                "latest_modified": 0,
                "harnesses_by_source": {},
            }
        sessions = sorted(group.sessions, key=modified_at, reverse=True)
        latest_modified = max(
            (modified_at(session) for session in sessions), default=0
        )
        project["session_count"] += group.session_count
        project["latest_modified"] = max(project["latest_modified"], latest_modified)
        harness = project["harnesses_by_source"].setdefault(
            source.id,
            {
                "source": source.id,
                "source_label": source.label,
                "groups": [],
                "session_count": 0,
                "latest_modified": 0,
                "sessions": [],
            },
        )
        harness["groups"].append(group.key)
        harness["session_count"] += group.session_count
        harness["latest_modified"] = max(
            harness["latest_modified"], latest_modified
        )
        harness["sessions"].extend(session.as_dict() for session in sessions)

    projects = list(projects_by_identity.values())
    for project in projects:
        project["harnesses"] = list(project.pop("harnesses_by_source").values())
        for harness in project["harnesses"]:
            harness["groups"].sort()
            harness["sessions"].sort(
                key=lambda session: (
                    session["modified"].timestamp() if session["modified"] else 0
                ),
                reverse=True,
            )
        project["harnesses"].sort(
            key=lambda harness: (
                -harness["latest_modified"],
                harness["source_label"].lower(),
            )
        )
    projects.sort(
        key=lambda project: (
            -project["latest_modified"],
            project["label"].lower(),
            project["directory"] or "",
        )
    )
    return projects


def detect_projects() -> list[dict]:
    """Discover transcripts as project -> harness -> sessions for the local UI."""
    discovered = [
        (source, group)
        for source in SOURCES
        for group in source.discover()
    ]
    return projects_from_groups(discovered)


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
    "detect_projects",
    "projects_from_groups",
    "find_session",
]
