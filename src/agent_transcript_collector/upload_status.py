"""Determine transcript upload states without performing redaction."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Protocol, TypeAlias

from botocore.exceptions import ClientError

from .prepare_archive import TRANSCRIPT_FORMAT_VERSION
from .redactor import REDACTION_VERSION
from .s3client import S3_BUCKET
from .transcript import TranscriptRef, TranscriptStatus
from .transcript_snapshot import (
    SOURCE_HASH_VERSION,
    TranscriptSnapshot,
    snapshot_transcript,
)

StatusCallback: TypeAlias = Callable[[int, int], None]


class S3HeadClient(Protocol):
    """Small part of the S3 client needed to classify uploads."""

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...


SOURCE_HASH_METADATA = "source-hash"
SOURCE_HASH_VERSION_METADATA = "source-hash-version"
REDACTION_VERSION_METADATA = "redaction-version"
FORMAT_VERSION_METADATA = "transcript-format-version"

# Read-only compatibility with uploads written before hashes were renamed.
LEGACY_SOURCE_HASH_METADATA = "content-fingerprint"
LEGACY_SOURCE_HASH_VERSION_METADATA = "fingerprint-version"


def metadata_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_METADATA_CONCURRENCY", "16")))


def hashing_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_REDACTION_CONCURRENCY", "16")))


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
        or metadata.get(FORMAT_VERSION_METADATA) != str(TRANSCRIPT_FORMAT_VERSION)
    ):
        return False
    return metadata.get(
        SOURCE_HASH_METADATA, metadata.get(LEGACY_SOURCE_HASH_METADATA)
    ) == snapshot.source_hash
