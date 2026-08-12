"""Disposable on-disk cache for transcript hashes and upload status.

The cache is an optimization, never authoritative state. Unknown or malformed
schemas are replaced with an empty cache instead of migrated.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal, TypedDict, cast

from .paths import pipeline_cache_path, prepared_artifacts_dir
from .prepare_archive import TRANSCRIPT_FORMAT_VERSION
from .redactor import REDACTION_VERSION
from .sources.base import Session
from .transcript import TranscriptRef, TranscriptStatus, UploadState
from .transcript_snapshot import (
    SOURCE_HASH_VERSION,
    FilesystemSnapshotEntry,
    filesystem_snapshot_is_current,
)


class CacheRecord(TypedDict, total=False):
    source: str
    contributor: str
    project: str
    parent: str | None
    session: str
    filesystem_snapshot: list[FilesystemSnapshotEntry]
    source_hash_version: int
    source_hash: str
    key: str
    redaction_version: int
    format_version: int
    state: str
    error: str


class CacheFile(TypedDict):
    records: dict[str, CacheRecord]


UploadDecision = Literal["skip", "upload", "stale"]


def empty_cache() -> CacheFile:
    return {"records": {}}


def cache_record_key(contributor: str, source_id: str, session: Session) -> str:
    """Return the stable cache key for one local transcript."""
    return json.dumps(
        [contributor, source_id, str(session.path), session.id, session.parent or ""],
        separators=(",", ":"),
    )


def get_cache(path: Path | None = None) -> CacheFile:
    """Load the full cache, or return an empty cache for any unusable file."""
    target = path or pipeline_cache_path()
    try:
        value = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        return empty_cache()
    if path is None and "cache_version" in value:
        _cleanup_legacy_archives()
    records = value.get("records")
    if not isinstance(records, dict):
        return empty_cache()
    if not all(isinstance(key, str) and isinstance(record, dict)
               for key, record in records.items()):
        return empty_cache()
    return {
        "records": cast(dict[str, CacheRecord], records),
    }


def get_cache_for_transcript(
    cache: CacheFile, contributor: str, source_id: str, session: Session
) -> CacheRecord | None:
    """Return one transcript record from an already-loaded cache."""
    record = cache["records"].get(cache_record_key(contributor, source_id, session))
    return record if isinstance(record, dict) else None


def reusable_status(
    cache: CacheFile, contributor: str, ref: TranscriptRef
) -> TranscriptStatus | None:
    """Return a status that can be reused without S3 or file reads."""
    record = get_cache_for_transcript(
        cache, contributor, ref.source.id, ref.session
    )
    state = record.get("state") if record else None
    if record is None or state not in {"changed", "current"}:
        return None
    if not _record_matches(record, ref):
        return None
    return TranscriptStatus(ref, cast(UploadState, state))


def upload_decision(
    cache: CacheFile, contributor: str, ref: TranscriptRef
) -> tuple[UploadDecision, CacheRecord | None]:
    """Decide whether a cached transcript is ready to upload."""
    record = get_cache_for_transcript(
        cache, contributor, ref.source.id, ref.session
    )
    if record is None:
        return "stale", None
    if record.get("state") == "not_uploaded":
        return "upload", record
    status = reusable_status(cache, contributor, ref)
    if status is None:
        return "stale", None
    if status.state == "current":
        return "skip", None
    return "upload", record


def store_status(
    cache: CacheFile, contributor: str, status: TranscriptStatus
) -> None:
    """Replace one cache record with the result of a status check."""
    ref = status.transcript
    record: CacheRecord = {
        "source": ref.source.id,
        "contributor": contributor,
        "project": ref.session.project_id,
        "parent": ref.session.parent,
        "session": ref.session.id,
        "key": ref.key,
        "state": status.state,
    }
    if status.snapshot is not None:
        record.update(
            source_hash_version=SOURCE_HASH_VERSION,
            redaction_version=REDACTION_VERSION,
            format_version=TRANSCRIPT_FORMAT_VERSION,
            filesystem_snapshot=status.snapshot.filesystem_snapshot or [],
            source_hash=status.snapshot.source_hash,
        )
    if status.error:
        record["error"] = status.error
    set_cache_for_transcript(
        cache, contributor, ref.source.id, ref.session, record
    )


def mark_records_uploaded(
    cache: CacheFile, contributor: str, identities: set[tuple[object, ...]]
) -> None:
    """Mark matching contributor records current after successful S3 writes."""
    for record in cache["records"].values():
        identity = (
            record.get("source"),
            record.get("project"),
            record.get("parent") or "",
            record.get("session"),
        )
        if record.get("contributor") == contributor and identity in identities:
            record["state"] = "current"
            record.pop("error", None)


def _record_matches(record: CacheRecord, ref: TranscriptRef) -> bool:
    return (
        record.get("source") == ref.source.id
        and record.get("project") == ref.session.project_id
        and record.get("parent") == ref.session.parent
        and record.get("session") == ref.session.id
        and record.get("key") == ref.key
        and record.get("source_hash_version") == SOURCE_HASH_VERSION
        and record.get("redaction_version") == REDACTION_VERSION
        and record.get("format_version") == TRANSCRIPT_FORMAT_VERSION
        and isinstance(record.get("source_hash"), str)
        and filesystem_snapshot_is_current(record.get("filesystem_snapshot"))
    )


def set_cache_for_transcript(
    cache: CacheFile,
    contributor: str,
    source_id: str,
    session: Session,
    record: CacheRecord,
) -> str:
    """Store one transcript record and return its cache key."""
    key = cache_record_key(contributor, source_id, session)
    cache["records"][key] = record
    return key


def save_cache(cache: CacheFile, path: Path | None = None) -> None:
    """Atomically persist the full cache with user-only permissions."""
    target = path or pipeline_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _cleanup_legacy_archives() -> None:
    """Remove private ZIPs left by the former prepared-artifact cache."""
    root = prepared_artifacts_dir()
    try:
        resolved_root = root.resolve()
        archives = list(root.rglob("transcript-*.zip"))
    except OSError:
        return
    for archive in archives:
        try:
            if archive.resolve().is_relative_to(resolved_root):
                archive.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            continue
