"""Small typed values shared by scan, status, cache, and upload code."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias
from uuid import uuid4

from .sources.base import Session, Source
from .storage import transcript_key
from .transcript_snapshot import TranscriptSnapshot

TranscriptIdentity: TypeAlias = tuple[str, str, str, str]
TranscriptSelections: TypeAlias = Iterable[tuple[Source, Iterable[Session]]]
UploadState: TypeAlias = Literal[
    "not_uploaded", "changed", "current", "error", "stale"
]


@dataclass(frozen=True)
class TranscriptRef:
    """A transcript plus the source adapter and S3 key needed to process it."""

    source: Source
    session: Session
    key: str
    transcript_id: str
    parent_transcript_id: str | None = None
    child_transcript_ids: tuple[tuple[str, str], ...] = ()

    @property
    def identity(self) -> TranscriptIdentity:
        return (
            self.source.id,
            self.session.project_id,
            self.session.parent or "",
            self.session.id,
        )


@dataclass(frozen=True)
class TranscriptStatus:
    """The upload state determined for one transcript."""

    transcript: TranscriptRef
    state: UploadState
    snapshot: TranscriptSnapshot | None = None
    error: str | None = None

    def as_item(self) -> dict[str, str | None]:
        source, project, _parent, session = self.transcript.identity
        return {
            "source": source,
            "project": project,
            "parent": self.transcript.session.parent,
            "session": session,
            "state": self.state,
        }


def transcript_refs(
    selections: TranscriptSelections,
    contributor: str,
    existing_records: Iterable[Mapping[str, object]] = (),
) -> list[TranscriptRef]:
    """Assign or reuse package IDs and attach their flat S3 keys."""
    sessions = [
        (source, session) for source, sessions in selections for session in sessions
    ]
    transcript_ids = {
        (
            str(record.get("source") or ""),
            str(record.get("project") or ""),
            str(record.get("session") or ""),
        ): transcript_id
        for record in existing_records
        if str(record.get("contributor") or "") == contributor
        if (transcript_id := str(record.get("transcript_id") or ""))
    }
    for source, session in sessions:
        transcript_ids.setdefault(
            (source.id, session.project_id, session.id), str(uuid4())
        )

    refs = []
    for source, session in sessions:
        transcript_id = transcript_ids[
            (source.id, session.project_id, session.id)
        ]
        parent_id = transcript_ids.get(
            (source.id, session.project_id, session.parent or "")
        )
        child_ids = tuple(
            (
                child_id,
                transcript_ids[(source.id, session.project_id, child_id)],
            )
            for child_id in session.child_ids
            if (source.id, session.project_id, child_id) in transcript_ids
        )
        refs.append(
            TranscriptRef(
                source,
                session,
                transcript_key(contributor, session, transcript_id),
                transcript_id,
                parent_id,
                child_ids,
            )
        )
    return refs
