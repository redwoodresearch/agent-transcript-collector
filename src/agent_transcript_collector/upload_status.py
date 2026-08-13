"""Determine transcript upload states without performing redaction."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

from botocore.exceptions import ClientError

from .cache import get_cache, reusable_status, save_cache, store_status
from .prepare_archive import MANIFEST_VERSION
from .redactor import REDACTION_VERSION
from .s3client import S3_BUCKET, make_s3_client
from .transcript import (
    TranscriptRef,
    TranscriptSelections,
    TranscriptStatus,
    transcript_refs,
)
from .transcript_snapshot import (
    SOURCE_HASH_VERSION,
    TranscriptSnapshot,
    snapshot_transcript,
)

StatusCallback: TypeAlias = Callable[[int, int], None]
ProgressCallback: TypeAlias = Callable[[str, int, int], None]


class S3HeadClient(Protocol):
    """Small part of the S3 client needed to classify uploads."""

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...


SOURCE_HASH_METADATA = "source-hash"
SOURCE_HASH_VERSION_METADATA = "source-hash-version"
REDACTION_VERSION_METADATA = "redaction-version"
MANIFEST_VERSION_METADATA = "mts-manifest-version"

# Read-only compatibility with uploads written before hashes were renamed.
LEGACY_SOURCE_HASH_METADATA = "content-fingerprint"
LEGACY_SOURCE_HASH_VERSION_METADATA = "fingerprint-version"


def metadata_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_METADATA_CONCURRENCY", "20")))


def hashing_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_HASH_CONCURRENCY", "16")))


def refresh_upload_status(
    selections: TranscriptSelections,
    contributor: str,
    *,
    s3: S3HeadClient | None = None,
    on_progress: ProgressCallback | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Reuse valid cache entries and classify everything else against S3."""
    refs = transcript_refs(selections, contributor)
    cache = get_cache(cache_path)
    cached: list[TranscriptStatus] = []
    unresolved: list[TranscriptRef] = []
    for ref in refs:
        status = reusable_status(cache, contributor, ref)
        if status is None:
            unresolved.append(ref)
        else:
            cached.append(status)

    if on_progress:
        on_progress("checking", 0, len(unresolved))

    def progress(stage: str) -> StatusCallback:
        return lambda done, total: on_progress(stage, done, total) if on_progress else None

    if unresolved:
        client = cast(S3HeadClient, s3 or make_s3_client())
        checked = classify_uploads(
            client,
            unresolved,
            on_check=progress("checking"),
            on_hash=progress("hashing"),
        )
    else:
        checked = []

    for status in checked:
        store_status(cache, contributor, status)
    save_cache(cache, cache_path)

    statuses = {status.transcript.identity: status for status in cached + checked}
    ordered = [statuses[ref.identity] for ref in refs if ref.identity in statuses]
    errors = [_status_error(status) for status in ordered if status.error]
    usable = any(
        status.state in {"not_uploaded", "changed", "current"}
        for status in ordered
    )
    return {
        "status": "partial" if errors and usable else "failed" if errors else "ready",
        "items": [status.as_item() for status in ordered],
        "errors": errors,
        "total": len(refs),
        "changed": sum(status.snapshot is not None for status in checked),
        "checked": len(unresolved),
        "cached": len(cached),
    }


def classify_uploads(
    s3: S3HeadClient,
    transcripts: Iterable[TranscriptRef],
    on_check: StatusCallback | None = None,
    on_hash: StatusCallback | None = None,
) -> list[TranscriptStatus]:
    """HEAD every key, then hash only objects which already exist in S3."""
    refs = list(transcripts)
    statuses: list[TranscriptStatus] = []
    existing: list[tuple[TranscriptRef, dict[str, str]]] = []

    def head_one(ref: TranscriptRef) -> dict[str, str] | None:
        try:
            response = s3.head_object(Bucket=S3_BUCKET, Key=ref.key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return response.get("Metadata", {})

    workers = min(metadata_concurrency(), len(refs))
    if workers:
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(head_one, ref): ref for ref in refs}
            for future in as_completed(futures):
                ref = futures[future]
                completed += 1
                try:
                    metadata = future.result()
                except Exception as exc:
                    statuses.append(_error_status(ref, exc))
                else:
                    if metadata is None:
                        statuses.append(TranscriptStatus(ref, "not_uploaded"))
                    else:
                        existing.append((ref, metadata))
                if on_check:
                    on_check(completed, len(refs))

    if on_hash:
        on_hash(0, len(existing))
    workers = min(hashing_concurrency(), len(existing))
    if workers:
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_classify_existing, ref, metadata): ref
                for ref, metadata in existing
            }
            for future in as_completed(futures):
                ref = futures[future]
                completed += 1
                try:
                    statuses.append(future.result())
                except Exception as exc:
                    statuses.append(_error_status(ref, exc))
                if on_hash:
                    on_hash(completed, len(existing))
    return statuses


def _classify_existing(
    ref: TranscriptRef, metadata: dict[str, str]
) -> TranscriptStatus:
    snapshot = snapshot_transcript(ref.source, ref.session, ref.key)
    state = "current" if upload_is_current(snapshot, metadata) else "changed"
    # Status checks need the hash and filesystem snapshot, not transcript contents.
    snapshot = replace(snapshot, raw_bytes=b"")
    return TranscriptStatus(ref, state, snapshot)


def _error_status(ref: TranscriptRef, exc: Exception) -> TranscriptStatus:
    return TranscriptStatus(ref, "error", error=f"{type(exc).__name__}: {exc}")


def _status_error(status: TranscriptStatus) -> dict[str, str]:
    return {
        "source": status.transcript.source.id,
        "project": status.transcript.session.project_id,
        "parent": status.transcript.session.parent or "",
        "session": status.transcript.session.id,
        "error": status.error or "Upload status unavailable",
    }


def upload_is_current(
    snapshot: TranscriptSnapshot, metadata: dict[str, str]
) -> bool:
    """Return whether an existing object matches local inputs and policies."""
    hash_version = metadata.get(
        SOURCE_HASH_VERSION_METADATA,
        metadata.get(LEGACY_SOURCE_HASH_VERSION_METADATA),
    )
    if (
        hash_version != str(SOURCE_HASH_VERSION)
        or metadata.get(REDACTION_VERSION_METADATA) != str(REDACTION_VERSION)
        or metadata.get(MANIFEST_VERSION_METADATA) != str(MANIFEST_VERSION)
    ):
        return False
    return metadata.get(
        SOURCE_HASH_METADATA, metadata.get(LEGACY_SOURCE_HASH_METADATA)
    ) == snapshot.source_hash
