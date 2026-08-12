"""Disposable on-disk cache for transcript hashes and upload status.

The cache is an optimization, never authoritative state. Unknown or malformed
schemas are replaced with an empty cache instead of migrated.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict, cast

from .paths import pipeline_cache_path, prepared_artifacts_dir


class FilesystemSnapshotEntry(TypedDict, total=False):
    path: str
    exists: bool
    size: int
    mtime_ns: int


class CacheRecord(TypedDict, total=False):
    source: str
    contributor: str
    group: str
    parent: str | None
    session: str
    filesystem_snapshot: list[FilesystemSnapshotEntry]
    source_hash_version: int
    transcript_hash: str
    source_hash: str
    key: str
    sidecar_count: int
    redaction_version: int
    format_version: int
    state: str
    error: str


class CacheFile(TypedDict):
    records: dict[str, CacheRecord]


def empty_cache() -> CacheFile:
    return {"records": {}}


def cache_record_key(contributor: str, source_id: str, session) -> str:
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
    cache: CacheFile, contributor: str, source_id: str, session
) -> CacheRecord | None:
    """Return one transcript record from an already-loaded cache."""
    record = cache["records"].get(cache_record_key(contributor, source_id, session))
    return record if isinstance(record, dict) else None


def set_cache_for_transcript(
    cache: CacheFile,
    contributor: str,
    source_id: str,
    session,
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
