"""Turn one local transcript and its sidecars into a redacted ZIP archive."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from .redactor import (
    REDACTION_VERSION,
    redact_identity,
    redact_jsonl_content,
    redact_path_token,
)
from .sources.base import Session, Source
from .transcript_snapshot import (
    SOURCE_HASH_VERSION,
    FilesystemSnapshotEntry,
    TranscriptSnapshot,
    filesystem_snapshot,
    snapshot_transcript,
)

TRANSCRIPT_FORMAT_VERSION = 4


class ArchiveArtifact(TypedDict):
    """The local ZIP and metadata passed to the S3 uploader."""

    source: str
    project: str
    session: str
    parent: str | None
    path: str
    key: str
    source_hash: str
    sidecar_count: int
    redactions: int
    zip_size_bytes: int
    filesystem_snapshot: list[FilesystemSnapshotEntry]


def _redact(text: str) -> tuple[str, int]:
    text, redaction_count = redact_jsonl_content(text)
    text, identity_count = redact_identity(text)
    return text, redaction_count + identity_count


def _redacted_sidecars(
    snapshot: TranscriptSnapshot,
) -> tuple[list[tuple[str, str]], dict, int]:
    contents: list[tuple[str, str]] = []
    entries = []
    missing = list(snapshot.sidecars.missing)
    redaction_count = 0
    for sidecar in snapshot.sidecars.files:
        try:
            text = sidecar.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            missing.append(sidecar.reference)
            continue
        text, count = _redact(text)
        redaction_count += count
        contents.append((sidecar.arcname, text))
        entries.append({
            "path": sidecar.arcname,
            "kind": sidecar.kind,
            "referenced_as": redact_identity(sidecar.reference)[0],
            "size_bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
    return contents, {
        "files": entries,
        "missing": [redact_identity(path)[0] for path in sorted(missing)],
        "skipped_too_large": [
            redact_identity(path)[0] for path in snapshot.sidecars.skipped
        ],
    }, redaction_count


def _archive_bytes(
    source: Source, snapshot: TranscriptSnapshot, contributor: str
) -> tuple[bytes, dict[str, Any]]:
    session = snapshot.session
    transcript, redaction_count = _redact(
        snapshot.raw_bytes.decode("utf-8", errors="replace")
    )
    sidecar_contents, sidecars, count = _redacted_sidecars(snapshot)
    redaction_count += count
    project_id, count = redact_path_token(session.project_id)
    redaction_count += count
    project_label, count = redact_identity(session.project_label)
    redaction_count += count
    suffix = Path(session.path).suffix.lower()
    if suffix not in {".jsonl", ".txt"}:
        suffix = ".txt"
    manifest = {
        "transcript_format_version": TRANSCRIPT_FORMAT_VERSION,
        "source": source.id,
        "source_format": source.source_format,
        "contributor": contributor,
        "project": {"key": project_id, "name": project_label},
        "session": {
            "id": session.id,
            "is_subagent": session.is_subagent,
            "parent": session.parent,
        },
        "version": {
            "source_hash": snapshot.source_hash,
            "source_hash_version": SOURCE_HASH_VERSION,
            "redaction_version": REDACTION_VERSION,
            "content_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        "size_bytes": len(transcript.encode("utf-8")),
        "redactions": redaction_count,
        "sidecars": sidecars,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr(f"transcript{suffix}", transcript)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for arcname, text in sidecar_contents:
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
        "path": temporary,
        "key": snapshot.key,
        "source_hash": snapshot.source_hash,
        "sidecar_count": len(manifest["sidecars"]["files"]),
        "redactions": manifest["redactions"],
        "zip_size_bytes": len(archive),
        "filesystem_snapshot": filesystem_snapshot(snapshot),
    }
