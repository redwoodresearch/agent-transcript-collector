"""Turn one local transcript and its sidecars into a redacted ZIP archive."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from .redactor import (
    REDACTION_VERSION,
    redact_identity,
    redact_jsonl_content,
    redact_path_token,
)
from .scan import load_transcript_inputs
from .sidecars import EMPTY as NO_SIDECARS
from .sidecars import SidecarSet
from .sources.base import Session, Source

TRANSCRIPT_FORMAT_VERSION = 4
SOURCE_HASH_VERSION = 3


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
    filesystem_snapshot: list[dict[str, Any]]


@dataclass(frozen=True)
class TranscriptSnapshot:
    """One stable, unredacted read of a transcript and its sidecars."""

    session: Session
    raw_bytes: bytes
    source_hash: str
    key: str
    sidecars: SidecarSet = NO_SIDECARS
    filesystem_snapshot: list[dict[str, Any]] | None = None


def source_hash(raw_bytes: bytes, sidecars: SidecarSet) -> str:
    """Identify the original transcript and all available sidecar content."""
    digest = hashlib.sha256(f"source-v{SOURCE_HASH_VERSION}\0".encode())
    digest.update(raw_bytes)
    if not sidecars.files:
        return digest.hexdigest()
    transcript_digest = digest.hexdigest()
    digest = hashlib.sha256(f"sidecars-v1\0{transcript_digest}".encode())
    for sidecar in sidecars.files:
        try:
            raw = sidecar.path.read_bytes()
        except OSError:
            raw = b""
        digest.update(sidecar.arcname.encode() + b"\0")
        digest.update(hashlib.sha256(raw).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _path_signature(path: str | Path) -> dict[str, Any]:
    value = {"path": str(path)}
    try:
        stat = Path(path).stat()
    except OSError:
        value["exists"] = False
    else:
        value.update(exists=True, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    return value


def filesystem_snapshot(snapshot: TranscriptSnapshot) -> list[dict[str, Any]]:
    """Capture every local path which can affect the archive."""
    paths: list[str | Path] = [snapshot.session.path]
    paths.extend(sidecar.path for sidecar in snapshot.sidecars.files)
    paths.extend(snapshot.sidecars.missing)
    paths.extend(snapshot.sidecars.skipped)
    paths.extend(snapshot.sidecars.directories)
    return [_path_signature(path) for path in dict.fromkeys(paths)]


def filesystem_snapshot_is_current(snapshot: object) -> bool:
    return isinstance(snapshot, list) and all(
        isinstance(item, dict)
        and item == _path_signature(str(item.get("path", "")))
        for item in snapshot
    )


def snapshot_transcript(
    source: Source, session: Session, key: str
) -> TranscriptSnapshot:
    """Read and hash one stable transcript/sidecar snapshot."""
    path = Path(session.path)
    for _attempt in range(2):
        transcript_before = _path_signature(path)
        inputs = load_transcript_inputs(source, session)
        if transcript_before != _path_signature(path):
            continue
        probe = TranscriptSnapshot(
            session=session,
            raw_bytes=inputs.raw_bytes,
            source_hash="",
            key=key,
            sidecars=inputs.sidecars,
        )
        snapshot = filesystem_snapshot(probe)
        snapshot_data = TranscriptSnapshot(
            session=session,
            raw_bytes=inputs.raw_bytes,
            source_hash=source_hash(inputs.raw_bytes, inputs.sidecars),
            key=key,
            sidecars=inputs.sidecars,
            filesystem_snapshot=snapshot,
        )
        if snapshot == filesystem_snapshot(snapshot_data):
            return snapshot_data
    raise RuntimeError("Transcript changed while it was being hashed")


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
    snapshot: TranscriptSnapshot,
    contributor: str,
    directory: str | Path,
) -> ArchiveArtifact:
    """Redact and write one transcript ZIP, returning its upload description."""
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
    session = snapshot.session
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
