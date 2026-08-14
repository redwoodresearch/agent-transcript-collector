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


def source_hash(
    raw_bytes: bytes,
    attachments: AttachmentSet,
    child_ids: tuple[str, ...] = (),
    *,
    child_refs: tuple[tuple[str, str | None], ...] = (),
) -> str:
    """Identify transcript, attachments, and derived subagent links."""
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
    links = child_refs or tuple((child_id, None) for child_id in child_ids)
    if links:
        content_digest = digest.hexdigest()
        digest = hashlib.sha256(f"subagent-links-v2\0{content_digest}".encode())
        for child_id, spawn_ref in sorted(
            set(links), key=lambda item: (item[0], item[1] or "")
        ):
            digest.update(child_id.encode() + b"\0")
            digest.update((spawn_ref or "").encode() + b"\0")
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
        )
        snapshot_before_hash = filesystem_snapshot(probe)
        try:
            hashed = source_hash(
                inputs.raw_bytes,
                inputs.attachments,
                session.child_ids,
                child_refs=session.child_refs,
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
            filesystem_snapshot=snapshot_after_hash,
        )
        if snapshot_after_hash == filesystem_snapshot(snapshot_data):
            return snapshot_data
    raise RuntimeError("Transcript changed while it was being hashed")
