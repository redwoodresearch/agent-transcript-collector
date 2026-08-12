"""Small typed values shared by scan, status, cache, and upload code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .prepare_archive import TranscriptSnapshot
from .sources.base import Session, Source

TranscriptIdentity: TypeAlias = tuple[str, str, str, str]
UploadState: TypeAlias = Literal[
    "not_uploaded", "changed", "current", "error", "stale"
]


@dataclass(frozen=True)
class TranscriptRef:
    """A transcript plus the source adapter and S3 key needed to process it."""

    source: Source
    session: Session
    key: str

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
