"""Classify S3 upload existence and freshness without redacting transcripts."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Protocol, TypeAlias

from botocore.exceptions import ClientError

from .prepare_archive import (
    SOURCE_HASH_VERSION,
    TRANSCRIPT_FORMAT_VERSION,
    TranscriptSnapshot,
)
from .redactor import REDACTION_VERSION
from .s3client import S3_BUCKET
from .sources.base import Session, Source
from .storage import transcript_key

MissingUpload: TypeAlias = tuple[Source, Session, str]
ExistingUpload: TypeAlias = tuple[Source, Session, str, dict[str, str]]
StatusError: TypeAlias = dict[str, Any]
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


def find_existing_uploads(
    s3: S3HeadClient,
    transcripts: Iterable[tuple[Source, Session]],
    contributor: str,
    on_status: StatusCallback | None = None,
) -> tuple[list[MissingUpload], list[ExistingUpload], list[StatusError]]:
    """HEAD transcript keys, returning (missing, existing, errors).

    `missing` contains `(source, session, key)` tuples. `existing` contains
    `(source, session, key, metadata)` tuples. No transcript file is read.
    """
    transcripts = list(transcripts)
    missing: list[MissingUpload] = []
    existing: list[ExistingUpload] = []
    errors: list[StatusError] = []

    def head_one(
        source: Source, session: Session
    ) -> tuple[str, dict[str, str] | None]:
        key = transcript_key(contributor, source.id, session)
        try:
            response = s3.head_object(Bucket=S3_BUCKET, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return key, None
            raise
        return key, response.get("Metadata", {})

    workers = min(metadata_concurrency(), len(transcripts))
    if not workers:
        return missing, existing, errors
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(head_one, source, session): (source, session)
            for source, session in transcripts
        }
        for future in as_completed(futures):
            source, session = futures[future]
            completed += 1
            try:
                key, metadata = future.result()
            except Exception as exc:
                errors.append({
                    "source": source.id,
                    "project": session.project_id,
                    "parent": session.parent,
                    "session": session.id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                target = missing if metadata is None else existing
                target.append(
                    (source, session, key)
                    if metadata is None
                    else (source, session, key, metadata)
                )
            if on_status:
                on_status(completed, len(transcripts))
    return missing, existing, errors


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
