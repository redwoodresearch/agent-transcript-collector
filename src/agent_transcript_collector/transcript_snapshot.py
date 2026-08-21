"""Read and hash stable, unredacted transcript inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .scan import load_transcript_inputs
from .attachments import EMPTY as NO_ATTACHMENTS
from .attachments import AttachmentSet
from .sources.base import Session, Source
from .system_prompt import capture_path as prompt_capture_path

# 6: the captured system prompt and injected memory are archive content, so a
# session whose capture arrives or changes after an upload has to be recognised
# as stale. Bumping this re-uploads everything once, which is the only way an
# archive sent before its prompt was captured ever gets one.
SOURCE_HASH_VERSION = 6


class FilesystemSnapshotEntry(TypedDict, total=False):
    path: str
    exists: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class TranscriptSnapshot:
    """One stable, unredacted read of a transcript and its attachments."""

    session: Session
    raw_bytes: bytes
    source_hash: str
    key: str
    attachments: AttachmentSet = NO_ATTACHMENTS
    filesystem_snapshot: list[FilesystemSnapshotEntry] | None = None
    capture: Path | None = None


def capture_bytes(source_id: str, session_id: str) -> bytes:
    """The captured prompt and memory for a session, as hash input.

    Absent is a value: a session with no capture yet hashes differently from
    the same session once one arrives, which is what makes the archive stale.
    """
    try:
        return prompt_capture_path(source_id, session_id).read_bytes()
    except OSError:
        return b""


def source_hash(
    raw_bytes: bytes,
    attachments: AttachmentSet,
    child_ids: tuple[str, ...] = (),
    capture: bytes = b"",
) -> str:
    """Identify transcript, attachments, subagent links, and captured prompt."""
    digest = hashlib.sha256(f"source-v{SOURCE_HASH_VERSION}\0".encode())
    digest.update(raw_bytes)
    if attachments.files:
        transcript_digest = digest.hexdigest()
        digest = hashlib.sha256(f"attachments-v1\0{transcript_digest}".encode())
        for attachment in attachments.files:
            raw = attachment.path.read_bytes()
            digest.update(attachment.arcname.encode() + b"\0")
            digest.update(hashlib.sha256(raw).digest())
            digest.update(b"\0")
    if child_ids:
        content_digest = digest.hexdigest()
        digest = hashlib.sha256(f"subagent-links-v1\0{content_digest}".encode())
        for child_id in sorted(set(child_ids)):
            digest.update(child_id.encode() + b"\0")
    if capture:
        linked_digest = digest.hexdigest()
        digest = hashlib.sha256(f"capture-v1\0{linked_digest}".encode())
        digest.update(capture)
    return digest.hexdigest()


def _path_signature(path: str | Path) -> FilesystemSnapshotEntry:
    value: FilesystemSnapshotEntry = {"path": str(path)}
    try:
        stat = Path(path).stat()
    except OSError:
        value["exists"] = False
    else:
        value.update(exists=True, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    return value


def filesystem_snapshot(snapshot: TranscriptSnapshot) -> list[FilesystemSnapshotEntry]:
    """Capture every local path which can affect the archive."""
    paths: list[str | Path] = [snapshot.session.path]
    paths.extend(attachment.path for attachment in snapshot.attachments.files)
    paths.extend(snapshot.attachments.missing)
    paths.extend(snapshot.attachments.skipped)
    paths.extend(snapshot.attachments.directories)
    if snapshot.capture is not None:
        paths.append(snapshot.capture)
    return [_path_signature(path) for path in dict.fromkeys(paths)]


def filesystem_snapshot_is_current(snapshot: object) -> bool:
    return isinstance(snapshot, list) and bool(snapshot) and all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and bool(item["path"])
        and item == _path_signature(str(item.get("path", "")))
        for item in snapshot
    )


def snapshot_transcript(
    source: Source, session: Session, key: str
) -> TranscriptSnapshot:
    """Read and hash one stable transcript/attachment snapshot."""
    path = Path(session.path)
    capture = prompt_capture_path(source.id, session.id)
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
            attachments=inputs.attachments,
            capture=capture,
        )
        snapshot_before_hash = filesystem_snapshot(probe)
        try:
            hashed = source_hash(
                inputs.raw_bytes,
                inputs.attachments,
                session.child_ids,
                capture_bytes(source.id, session.id),
            )
        except OSError:
            continue
        snapshot_after_hash = filesystem_snapshot(probe)
        if snapshot_before_hash != snapshot_after_hash:
            continue
        snapshot_data = TranscriptSnapshot(
            session=session,
            raw_bytes=inputs.raw_bytes,
            source_hash=hashed,
            key=key,
            attachments=inputs.attachments,
            capture=capture,
            filesystem_snapshot=snapshot_after_hash,
        )
        if snapshot_after_hash == filesystem_snapshot(snapshot_data):
            return snapshot_data
    raise RuntimeError("Transcript changed while it was being hashed")
