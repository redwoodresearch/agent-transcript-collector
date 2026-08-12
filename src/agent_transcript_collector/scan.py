"""Unified discovery of local transcripts, subagents, projects, and sidecars."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .sidecars import SidecarSet
from .sources import SOURCES
from .sources.base import Group, Session, Source, session_sidecars

SessionKey = tuple[str, str, str | None, str]
GroupKey = tuple[str, str]
ScanProgress = Callable[[int, int, Source, int], None]


@dataclass(frozen=True)
class TranscriptInputs:
    """Original bytes and sidecars which determine one uploaded transcript."""

    raw_bytes: bytes
    sidecars: SidecarSet

    @property
    def text(self) -> str:
        return self.raw_bytes.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ScanResult:
    """One complete, indexed snapshot of discovered local transcripts."""

    sources: dict[str, Source]
    groups_by_source: dict[str, list[Group]]
    sessions: dict[SessionKey, Session]
    projects: list[dict]

    def find_session(
        self, source: str, group: str, session: str, parent: str | None = None
    ) -> Session | None:
        return self.sessions.get((source, group, parent or None, session))

    def resolve_sessions(
        self, selected: Iterable[dict]
    ) -> list[tuple[Source, list[Session]]]:
        """Resolve untrusted UI descriptors against this immutable scan."""
        picks: dict[str, set[tuple[str, str | None, str]]] = defaultdict(set)
        for item in selected:
            picks[str(item.get("source", ""))].add((
                str(item.get("group", "")),
                item.get("parent") or None,
                str(item.get("session", "")),
            ))
        selections = []
        for source_id, identities in picks.items():
            source = self.sources.get(source_id)
            if source is None:
                continue
            sessions = [
                session
                for group, parent, session_id in identities
                if (session := self.find_session(
                    source_id, group, session_id, parent
                )) is not None
            ]
            if sessions:
                selections.append((source, sessions))
        return selections

    def sessions_for_groups(
        self, allowed: set[GroupKey]
    ) -> list[tuple[Source, list[Session]]]:
        """Return sessions grouped by source for selected source/group pairs."""
        selections = []
        for source_id, groups in self.groups_by_source.items():
            sessions = [
                session
                for group in groups
                if (source_id, group.key) in allowed
                for session in group.sessions
            ]
            if sessions:
                selections.append((self.sources[source_id], sessions))
        return selections


def scan_transcripts(
    sources: Iterable[Source] | None = None,
    on_progress: ScanProgress | None = None,
) -> ScanResult:
    """Discover all sources once and build the shared indexes consumers need."""
    source_list = list(SOURCES if sources is None else sources)
    groups_by_source = {}
    sessions = {}
    discovered = []
    for position, source in enumerate(source_list, start=1):
        if on_progress:
            on_progress(position - 1, len(source_list), source, len(sessions))
        groups = source.discover()
        groups_by_source[source.id] = groups
        discovered.extend((source, group) for group in groups)
        for group in groups:
            for session in group.sessions:
                key = (source.id, group.key, session.parent or None, session.id)
                sessions[key] = session
        if on_progress:
            on_progress(position, len(source_list), source, len(sessions))
    return ScanResult(
        sources={source.id: source for source in source_list},
        groups_by_source=groups_by_source,
        sessions=sessions,
        projects=_projects_from_groups(discovered),
    )


def load_transcript_inputs(source: Source, session: Session) -> TranscriptInputs:
    """Read one original transcript and resolve every referenced sidecar."""
    raw_bytes = session.path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    return TranscriptInputs(
        raw_bytes=raw_bytes,
        sidecars=session_sidecars(source, session, text),
    )


def project_groups(project: dict) -> set[GroupKey]:
    """Return every source/group pair represented by one project dictionary."""
    return {
        (harness["source"], group)
        for harness in project["harnesses"]
        for group in harness["groups"]
    }


def _projects_from_groups(discovered: list[tuple[Source, Group]]) -> list[dict]:
    """Build the local UI's project tree from one discovery snapshot."""

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
                "identity": identity,
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
