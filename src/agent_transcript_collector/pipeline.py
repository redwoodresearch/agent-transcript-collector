"""Persistent scan-to-upload state shared by the Web UI and hourly watcher.

The pipeline has one durable record per local transcript and contributor. A
cheap filesystem snapshot decides whether hashing is necessary. New or
changed records are read, hashed, and reconciled with S3 without running
redaction. Uploads redact and package only the confirmed pending records.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeAlias

from .cache import (
    CacheRecord,
    cache_record_key,
    get_cache,
    get_cache_for_transcript,
    save_cache,
    set_cache_for_transcript,
)
from .prepare_archive import (
    SOURCE_HASH_VERSION,
    TRANSCRIPT_FORMAT_VERSION,
    ArchiveArtifact,
    TranscriptSnapshot,
    filesystem_snapshot,
    filesystem_snapshot_is_current,
    prepare_archive,
    snapshot_transcript,
)
from .redactor import REDACTION_VERSION
from .s3client import make_s3_client
from .sources.base import Session, Source
from .storage import transcript_key
from .upload_status import (
    find_existing_uploads,
    upload_is_current,
)

Selections: TypeAlias = Iterable[tuple[Source, Iterable[Session]]]
PipelineItem: TypeAlias = dict[str, Any]
ProgressCallback: TypeAlias = Callable[[str, int, int], None]
PreparationCallback: TypeAlias = Callable[[int, int], None]


def redaction_concurrency() -> int:
    """Return the number of transcripts prepared in parallel."""
    return max(1, int(os.environ.get("CTC_REDACTION_CONCURRENCY", "16")))


def _item(source_id: str, session: Session, status: str) -> PipelineItem:
    return {
        "source": source_id,
        "project": session.project_id,
        "parent": session.parent,
        "session": session.id,
        "state": status,
    }


def _record_is_current(
    record: CacheRecord, source_id: str, session: Session
) -> bool:
    return (
        record.get("source") == source_id
        and record.get("project") == session.project_id
        and record.get("parent") == session.parent
        and record.get("session") == session.id
        and record.get("source_hash_version") == SOURCE_HASH_VERSION
        and record.get("redaction_version") == REDACTION_VERSION
        and record.get("format_version") == TRANSCRIPT_FORMAT_VERSION
        and isinstance(record.get("source_hash"), str)
        and isinstance(record.get("key"), str)
        and filesystem_snapshot_is_current(record.get("filesystem_snapshot"))
    )


def _snapshot_existing(
    source: Source,
    session: Session,
    key: str,
    metadata: dict[str, str],
    contributor: str,
) -> tuple[TranscriptSnapshot, CacheRecord, dict[str, str]]:
    """Hash one existing S3 object's local transcript without shared mutation."""
    snapshot = snapshot_transcript(source, session, key)
    record = {
        "source": source.id,
        "contributor": contributor,
        "project": session.project_id,
        "parent": session.parent,
        "session": session.id,
        "source_hash_version": SOURCE_HASH_VERSION,
        "redaction_version": REDACTION_VERSION,
        "format_version": TRANSCRIPT_FORMAT_VERSION,
        "filesystem_snapshot": snapshot.filesystem_snapshot,
        "source_hash": snapshot.source_hash,
        "key": key,
        "state": "checking",
    }
    return replace(snapshot, raw_bytes=b""), record, metadata


def refresh(
    selections: Selections,
    contributor: str,
    *,
    s3: Any = None,
    on_progress: ProgressCallback | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Check S3 existence first, then hash only transcripts with uploads."""
    selections = [(source, list(sessions)) for source, sessions in selections]
    all_sessions = [
        (source, session)
        for source, sessions in selections
        for session in sessions
    ]
    cache = get_cache(cache_path)
    records = cache["records"]
    unchecked = []
    snapshots_by_key = {}
    items_by_key = {}

    for source, session in all_sessions:
        key = cache_record_key(contributor, source.id, session)
        record = get_cache_for_transcript(cache, contributor, source.id, session)
        status = record.get("state") if record is not None else None
        if (record is not None and status in {"changed", "current"}
                and _record_is_current(record, source.id, session)):
            items_by_key[key] = _item(source.id, session, status)
        else:
            unchecked.append((key, source, session))

    errors = []
    client = (s3 or make_s3_client()) if unchecked else None
    if on_progress:
        on_progress("checking", 0, len(unchecked))

    def checked(done, total):
        if on_progress:
            on_progress("checking", done, total)

    missing, existing, check_errors = find_existing_uploads(
        client,
        [(source, session) for _, source, session in unchecked],
        contributor,
        on_status=checked,
    ) if unchecked else ([], [], [])
    errors.extend(check_errors)

    keys_by_identity = {
        (source.id, session.project_id, session.parent or "", session.id): cache_key
        for cache_key, source, session in unchecked
    }
    for source, session, object_key in missing:
        cache_key = keys_by_identity[
            (source.id, session.project_id, session.parent or "", session.id)
        ]
        record = {
            "source": source.id,
            "contributor": contributor,
            "project": session.project_id,
            "parent": session.parent,
            "session": session.id,
            "key": object_key,
            "state": "not_uploaded",
        }
        set_cache_for_transcript(cache, contributor, source.id, session, record)
        items_by_key[cache_key] = _item(source.id, session, "not_uploaded")

    failed = {
        (
            item.get("source"), item.get("project"),
            item.get("parent") or "", item.get("session"),
        )
        for item in check_errors
    }
    for cache_key, source, session in unchecked:
        identity = (
            source.id, session.project_id, session.parent or "", session.id
        )
        if identity in failed:
            set_cache_for_transcript(
                cache,
                contributor,
                source.id,
                session,
                {
                    "source": source.id,
                    "contributor": contributor,
                    "project": session.project_id,
                    "parent": session.parent,
                    "session": session.id,
                    "state": "error",
                    "error": next(
                        item["error"] for item in check_errors
                        if (
                            item.get("source"), item.get("project"),
                            item.get("parent") or "", item.get("session"),
                        ) == identity
                    ),
                },
            )
            items_by_key[cache_key] = _item(source.id, session, "error")

    if on_progress:
        on_progress("hashing", 0, len(existing))
    workers = max(1, min(redaction_concurrency(), len(existing)))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _snapshot_existing, source, session, key, metadata, contributor
            ):
                (source, session)
            for source, session, key, metadata in existing
        }
        for future in as_completed(futures):
            source, session = futures[future]
            cache_key = keys_by_identity[
                (source.id, session.project_id, session.parent or "", session.id)
            ]
            completed += 1
            try:
                snapshot, record, metadata = future.result()
            except Exception as exc:
                set_cache_for_transcript(
                    cache,
                    contributor,
                    source.id,
                    session,
                    {
                        "source": source.id,
                        "contributor": contributor,
                        "project": session.project_id,
                        "parent": session.parent,
                        "session": session.id,
                        "state": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                items_by_key[cache_key] = _item(source.id, session, "error")
                errors.append({"source": source.id, "session": session.id,
                               "error": f"{type(exc).__name__}: {exc}"})
            else:
                set_cache_for_transcript(
                    cache, contributor, source.id, session, record
                )
                snapshots_by_key[cache_key] = (snapshot, metadata)
            if on_progress:
                on_progress("hashing", completed, len(existing))

    for cache_key, (snapshot, metadata) in snapshots_by_key.items():
        record = records[cache_key]
        record["state"] = (
            "current" if upload_is_current(snapshot, metadata) else "changed"
        )
        items_by_key[cache_key] = _item(
            record["source"], snapshot.session, record["state"]
        )

    save_cache(cache, cache_path)
    items = []
    for source, session in all_sessions:
        key = cache_record_key(contributor, source.id, session)
        item = items_by_key.get(key)
        if item is not None:
            items.append(item)
    usable = any(
        item["state"] in {"not_uploaded", "changed", "current"}
        for item in items
    )
    return {
        "status": "partial" if errors and usable else "failed" if errors else "ready",
        "items": items,
        "errors": errors,
        "total": len(all_sessions),
        "changed": len(existing),
        "checked": len(unchecked),
        "cached": len(all_sessions) - len(unchecked),
    }


def artifacts_for(
    selections: Selections,
    contributor: str,
    cache_path: Path | None = None,
) -> tuple[list[CacheRecord], list[PipelineItem]]:
    """Return pending upload candidates or sessions requiring Refresh."""
    cache = get_cache(cache_path)
    candidates = []
    stale = []
    for source, sessions in selections:
        for session in sessions:
            record = get_cache_for_transcript(
                cache, contributor, source.id, session
            )
            if (
                record is not None
                and record.get("state") == "current"
                and _record_is_current(record, source.id, session)
            ):
                continue
            if (
                record is not None
                and (
                    record.get("state") == "not_uploaded"
                    or (
                        record.get("state") == "changed"
                        and _record_is_current(record, source.id, session)
                    )
                )
            ):
                candidates.append(dict(record))
            else:
                stale.append(_item(source.id, session, "stale"))
    return candidates, stale


def prepare_upload_artifacts(
    selections: Selections,
    candidates: list[CacheRecord],
    contributor: str,
    directory: str | Path,
    on_progress: PreparationCallback | None = None,
) -> tuple[list[ArchiveArtifact], list[dict[str, str]]]:
    """Revalidate, redact, and package candidates selected for upload."""
    wanted = {
        (item.get("source"), item.get("project"), item.get("parent") or "",
         item.get("session")): item
        for item in candidates
    }
    work = []
    resolved = set()
    for source, sessions in selections:
        for session in sessions:
            identity = (source.id, session.project_id, session.parent or "", session.id)
            if identity in wanted and identity not in resolved:
                work.append((source, session, wanted[identity]))
                resolved.add(identity)

    artifacts = []
    errors = [
        {
            "source": str(identity[0] or ""),
            "session": str(identity[3] or ""),
            "error": "Upload candidate is no longer available; refresh and try again",
        }
        for identity in wanted
        if identity not in resolved
    ]

    def prepare_one(
        source: Source, session: Session, candidate: CacheRecord
    ) -> ArchiveArtifact:
        key = transcript_key(contributor, source.id, session)
        snapshot = snapshot_transcript(source, session, key)
        previously_hashed = candidate.get("state") == "changed"
        if previously_hashed and (
            snapshot.source_hash != candidate.get("source_hash")
            or snapshot.filesystem_snapshot != candidate.get("filesystem_snapshot")
        ):
            raise RuntimeError("Transcript changed after Refresh; refresh and try again")
        artifact = prepare_archive(
            source, snapshot, contributor, directory
        )
        if filesystem_snapshot(snapshot) != snapshot.filesystem_snapshot:
            Path(artifact["path"]).unlink(missing_ok=True)
            raise RuntimeError("Transcript changed while its archive was prepared")
        return artifact

    total = len(wanted)
    workers = max(1, min(redaction_concurrency(), len(work)))
    completed = len(errors)
    if on_progress and completed:
        on_progress(completed, total)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(prepare_one, source, session, candidate):
                (source, session)
            for source, session, candidate in work
        }
        for future in as_completed(futures):
            source, session = futures[future]
            completed += 1
            try:
                artifacts.append(future.result())
            except Exception as exc:
                errors.append({
                    "source": source.id,
                    "session": session.id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            if on_progress:
                on_progress(completed, total)
    return artifacts, errors


def mark_uploaded(
    artifacts: list[ArchiveArtifact],
    contributor: str,
    cache_path: Path | None = None,
) -> None:
    cache = get_cache(cache_path)
    records = cache["records"]
    uploaded = {
        (item.get("source"), item.get("project"), item.get("parent") or "",
         item.get("session"))
        for item in artifacts
    }
    for record in records.values():
        if record.get("contributor") != contributor:
            continue
        identity = (
            record.get("source"), record.get("project"), record.get("parent") or "",
            record.get("session"),
        )
        if identity in uploaded:
            record["state"] = "current"
            record.pop("error", None)
    save_cache(cache, cache_path)
