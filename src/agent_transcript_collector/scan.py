"""Unified discovery of local projects, transcripts, subagents, and attachments."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from .attachments import AttachmentSet
from .sources import SOURCES
from .sources.base import Session, Source, session_attachments

SessionKey = tuple[str, str, str | None, str]
ScanProgress = Callable[[int, int, Source, int], None]


@dataclass(frozen=True)
class TranscriptInputs:
    raw_bytes: bytes
    attachments: AttachmentSet

    @property
    def text(self) -> str:
        return self.raw_bytes.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Project:
    identity: str
    label: str
    directory: str | None
    transcripts: tuple[Session, ...]

    def as_dict(self, sources: dict[str, Source]) -> dict:
        """Return the template view, deriving harness sections when rendered."""
        by_source: dict[str, list[Session]] = defaultdict(list)
        for transcript in self.transcripts:
            by_source[transcript.source].append(transcript)
        harnesses = []
        for source_id, transcripts in by_source.items():
            ordered = sorted(transcripts, key=_modified_at, reverse=True)
            harnesses.append({
                "source": source_id,
                "source_label": sources[source_id].label,
                "session_count": len(ordered),
                "latest_modified": max(map(_modified_at, ordered), default=0),
                "sessions": [transcript.as_dict() for transcript in ordered],
            })
        harnesses.sort(
            key=lambda item: (-item["latest_modified"], item["source_label"].lower())
        )
        return {
            "key": self.identity,
            "identity": self.identity,
            "label": self.label,
            "directory": self.directory,
            "session_count": len(self.transcripts),
            "latest_modified": max(map(_modified_at, self.transcripts), default=0),
            "harnesses": harnesses,
        }


@dataclass(frozen=True)
class ScanResult:
    sources: dict[str, Source]
    transcripts: tuple[Session, ...]
    sessions: dict[SessionKey, Session]
    projects: tuple[Project, ...]

    @property
    def project_dicts(self) -> list[dict]:
        return [project.as_dict(self.sources) for project in self.projects]

    def find_session(
        self, source: str, project: str, session: str, parent: str | None = None
    ) -> Session | None:
        return self.sessions.get((source, project, parent or None, session))

    def resolve_sessions(self, selected: Iterable[dict]):
        """Resolve untrusted UI descriptors against this immutable scan."""
        picks: dict[str, set[tuple[str, str | None, str]]] = defaultdict(set)
        for item in selected:
            picks[str(item.get("source", ""))].add((
                str(item.get("project", "")),
                item.get("parent") or None,
                str(item.get("session", "")),
            ))
        selections = []
        for source_id, identities in picks.items():
            source = self.sources.get(source_id)
            if source is None:
                continue
            transcripts = [
                transcript
                for project, parent, session_id in identities
                if (transcript := self.find_session(
                    source_id, project, session_id, parent
                )) is not None
            ]
            if transcripts:
                selections.append((source, transcripts))
        return selections

    def sessions_for_projects(self, identities: set[str]):
        """Return selected project transcripts grouped by source."""
        selected = {
            id(transcript)
            for project in self.projects
            if project.identity in identities
            for transcript in project.transcripts
        }
        by_source: dict[str, list[Session]] = defaultdict(list)
        for transcript in self.transcripts:
            if id(transcript) in selected:
                by_source[transcript.source].append(transcript)
        return [
            (self.sources[source_id], transcripts)
            for source_id, transcripts in by_source.items()
            if transcripts
        ]


def scan_transcripts(
    sources: Iterable[Source] | None = None,
    on_progress: ScanProgress | None = None,
) -> ScanResult:
    """Discover each adapter once and merge its transcripts into projects."""
    source_list = list(SOURCES if sources is None else sources)
    transcripts = []
    for position, source in enumerate(source_list, start=1):
        if on_progress:
            on_progress(position - 1, len(source_list), source, len(transcripts))
        transcripts.extend(source.discover())
        if on_progress:
            on_progress(position, len(source_list), source, len(transcripts))
    sources_by_id = {source.id: source for source in source_list}
    projects = tuple(_merge_projects(transcripts))
    normalized = tuple(
        transcript for project in projects for transcript in project.transcripts
    )
    sessions = {
        (item.source, item.project_id, item.parent or None, item.id): item
        for item in normalized
    }
    return ScanResult(
        sources=sources_by_id,
        transcripts=normalized,
        sessions=sessions,
        projects=projects,
    )


def load_transcript_inputs(source: Source, session: Session) -> TranscriptInputs:
    raw_bytes = session.path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    return TranscriptInputs(
        raw_bytes=raw_bytes,
        attachments=session_attachments(source, session, text),
    )


def _modified_at(session: Session) -> float:
    return session.modified.timestamp() if session.modified else 0


def _merge_projects(transcripts: list[Session]) -> list[Project]:
    """Merge source transcripts that describe the same repository/project."""
    directories_by_label: dict[str, set[str]] = defaultdict(set)
    for transcript in transcripts:
        if transcript.project_directory:
            directories_by_label[transcript.project_label].add(
                transcript.project_directory
            )

    merged: dict[str, list[Session]] = defaultdict(list)
    project_info: dict[str, tuple[str, str | None]] = {}
    for transcript in transcripts:
        directory = transcript.project_directory
        candidates = directories_by_label.get(transcript.project_label, set())
        if directory is None and len(candidates) == 1:
            directory = next(iter(candidates))
        identity = (
            f"directory:{directory}"
            if directory
            else f"unresolved:{transcript.project_label}"
        )
        merged[identity].append(replace(transcript, project_id=identity))
        project_info.setdefault(identity, (transcript.project_label, directory))

    projects = [
        Project(identity, project_info[identity][0], project_info[identity][1],
                tuple(items))
        for identity, items in merged.items()
    ]
    projects.sort(
        key=lambda project: (
            -max(map(_modified_at, project.transcripts), default=0),
            project.label.lower(),
            project.directory or "",
        )
    )
    return projects
