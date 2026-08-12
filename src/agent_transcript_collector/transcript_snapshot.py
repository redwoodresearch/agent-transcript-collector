"""Read and hash stable, unredacted transcript inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .scan import load_transcript_inputs
from .sidecars import EMPTY as NO_SIDECARS
from .sidecars import SidecarSet
from .sources.base import Session, Source

SOURCE_HASH_VERSION = 3


class FilesystemSnapshotEntry(TypedDict, total=False):
    path: str
    exists: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class TranscriptSnapshot:
    """One stable, unredacted read of a transcript and its sidecars."""

    session: Session
    raw_bytes: bytes
    source_hash: str
    key: str
    sidecars: SidecarSet = NO_SIDECARS
    filesystem_snapshot: list[FilesystemSnapshotEntry] | None = None


def source_hash(raw_bytes: bytes, sidecars: SidecarSet) -> str:
    """Identify the original transcript and all available sidecar content."""
    digest = hashlib.sha256(f"source-v{SOURCE_HASH_VERSION}\0".encode())
    digest.update(raw_bytes)
    if not sidecars.files:
        return digest.hexdigest()
    transcript_digest = digest.hexdigest()
    digest = hashlib.sha256(f"sidecars-v1\0{transcript_digest}".encode())
    for sidecar in sidecars.files:
        raw = sidecar.path.read_bytes()
        digest.update(sidecar.arcname.encode() + b"\0")
        digest.update(hashlib.sha256(raw).digest())
        digest.update(b"\0")
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
    paths.extend(sidecar.path for sidecar in snapshot.sidecars.files)
    paths.extend(snapshot.sidecars.missing)
    paths.extend(snapshot.sidecars.skipped)
    paths.extend(snapshot.sidecars.directories)
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
        snapshot_before_hash = filesystem_snapshot(probe)
        try:
            hashed = source_hash(inputs.raw_bytes, inputs.sidecars)
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
            sidecars=inputs.sidecars,
            filesystem_snapshot=snapshot_after_hash,
        )
        if snapshot_after_hash == filesystem_snapshot(snapshot_data):
            return snapshot_data
    raise RuntimeError("Transcript changed while it was being hashed")
