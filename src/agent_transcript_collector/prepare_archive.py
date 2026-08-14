"""Turn one local transcript and its attachments into a redacted ZIP archive."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import TypedDict

from .atif import ATIF_FILENAME, derive_atif
from .redactor import (
    REDACTION_VERSION,
    redact_identity,
    redact_jsonl_content,
)
from .sources.base import Session, Source
from .storage import transcript_filename, transcript_id
from .transcript_snapshot import (
    FilesystemSnapshotEntry,
    TranscriptSnapshot,
    filesystem_snapshot,
    snapshot_transcript,
)

MANIFEST_VERSION = 7


class ArchiveArtifact(TypedDict):
    """The local ZIP and metadata passed to the S3 uploader."""

    source: str
    project: str
    session: str
    parent: str | None
    transcript_id: str
    parent_transcript_id: str | None
    child_ids: tuple[str, ...]
    path: str
    key: str
    source_hash: str
    attachment_count: int
    redactions: int
    zip_size_bytes: int
    filesystem_snapshot: list[FilesystemSnapshotEntry]


def _redact(text: str) -> tuple[str, int]:
    text, redaction_count = redact_jsonl_content(text)
    text, identity_count = redact_identity(text)
    return text, redaction_count + identity_count


def _redacted_attachments(
    snapshot: TranscriptSnapshot,
) -> tuple[list[tuple[str, str]], int]:
    contents: list[tuple[str, str]] = []
    redaction_count = 0
    for attachment in snapshot.attachments.files:
        text = attachment.path.read_text(encoding="utf-8", errors="replace")
        text, count = _redact(text)
        redaction_count += count
        contents.append((attachment.arcname, text))
    return contents, redaction_count


def _archive_bytes(
    source: Source, snapshot: TranscriptSnapshot, contributor: str
) -> tuple[bytes, dict]:
    session = snapshot.session
    transcript, redaction_count = _redact(
        snapshot.raw_bytes.decode("utf-8", errors="replace")
    )
    attachment_contents, count = _redacted_attachments(snapshot)
    redaction_count += count
    project_label, count = redact_identity(session.project_label)
    redaction_count += count
    suffix = Path(session.path).suffix.lower()
    if suffix not in {".jsonl", ".txt"}:
        suffix = ".txt"
    transcript_name = f"transcript{suffix}"
    identity = transcript_id(source.id, session.id, session.parent)
    parent_identity = (
        transcript_id(source.id, session.parent) if session.parent else None
    )
    atif, _atif_manifest = derive_atif(
        source.id,
        transcript,
        transcript_name,
        identity,
        parent_identity,
        subagent_refs=[
            {
                "trajectory_id": transcript_id(source.id, child_id, session.id),
                "session_id": child_id,
                "trajectory_path": transcript_filename(
                    source.id, child_id, session.id
                ),
            }
            for child_id in session.child_ids
        ],
    )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "id": identity,
        "format": source.source_format,
        "source": {
            "type": source.id,
            "id": session.id,
        },
        "collection": {
            "type": "project",
            "contributor": contributor,
            "name": project_label,
        },
        "redaction": {
            "policy": f"agent-transcript-collector/{REDACTION_VERSION}",
            "count": redaction_count,
        },
    }
    if parent_identity:
        manifest["parent_id"] = parent_identity
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr(transcript_name, transcript)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        if atif is not None:
            archive.writestr(ATIF_FILENAME, atif)
        for arcname, text in attachment_contents:
            archive.writestr(arcname, text)
    return buffer.getvalue(), manifest


def prepare_archive(
    source: Source,
    session: Session,
    key: str,
    contributor: str,
    directory: str | Path,
    *,
    expected_hash: str | None = None,
    expected_filesystem_snapshot: list[FilesystemSnapshotEntry] | None = None,
) -> ArchiveArtifact:
    """Read, validate, redact, and ZIP one stable transcript."""
    snapshot = snapshot_transcript(source, session, key)
    if expected_hash is not None and (
        snapshot.source_hash != expected_hash
        or snapshot.filesystem_snapshot != expected_filesystem_snapshot
    ):
        raise RuntimeError("Transcript changed after Refresh; refresh and try again")

    archive, manifest = _archive_bytes(source, snapshot, contributor)
    identity = transcript_id(source.id, session.id, session.parent)
    parent_identity = (
        transcript_id(source.id, session.parent) if session.parent else None
    )
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="transcript-", suffix=".zip", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(archive)
        os.chmod(temporary, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    if filesystem_snapshot(snapshot) != snapshot.filesystem_snapshot:
        Path(temporary).unlink(missing_ok=True)
        raise RuntimeError("Transcript changed while its archive was prepared")

    return {
        "source": source.id,
        "project": session.project_id,
        "session": session.id,
        "parent": session.parent,
        "transcript_id": identity,
        "parent_transcript_id": parent_identity,
        "child_ids": session.child_ids,
        "path": temporary,
        "key": snapshot.key,
        "source_hash": snapshot.source_hash,
        "attachment_count": len(snapshot.attachments.files),
        "redactions": manifest["redaction"]["count"],
        "zip_size_bytes": len(archive),
        "filesystem_snapshot": filesystem_snapshot(snapshot),
    }
