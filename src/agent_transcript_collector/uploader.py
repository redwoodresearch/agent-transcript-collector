"""Upload one overwrite-in-place ZIP per transcript using ``mts-trans``."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Protocol, TypeAlias

from .paths import upload_lock_path
from .prepare_archive import (
    TRANSCRIPT_FORMAT_VERSION,
    ArchiveArtifact,
)
from .redactor import REDACTION_VERSION
from .s3client import S3_BUCKET
from .transcript_snapshot import SOURCE_HASH_VERSION
from .upload_status import (
    FORMAT_VERSION_METADATA,
    REDACTION_VERSION_METADATA,
    SOURCE_HASH_METADATA,
    SOURCE_HASH_VERSION_METADATA,
)

UploadResult: TypeAlias = dict[str, Any]
UploadError: TypeAlias = dict[str, str]


class S3UploadClient(Protocol):
    """Small part of the S3 client needed to upload an archive."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

MTS_FORMAT_VERSION_METADATA = "mts-format-version"
MTS_TRANSCRIPT_ID_METADATA = "mts-transcript-id"
MTS_TRANSCRIPT_KIND_METADATA = "mts-transcript-kind"
MTS_PARENT_TRANSCRIPT_ID_METADATA = "mts-parent-transcript-id"
MTS_PARENT_OBJECT_KEY_METADATA = "mts-parent-object-key"
MTS_SOURCE_METADATA = "mts-source"
MTS_COLLECTION_TYPE_METADATA = "mts-collection-type"
CONTENT_SHA256_METADATA = "content-sha256"


def upload_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_UPLOAD_CONCURRENCY", "8")))


class UploadBusy(RuntimeError):
    """Raised when another collector process owns the upload lock."""


class UploadLock:
    """Cross-process advisory lock covering manual and scheduled uploads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or upload_lock_path()
        self._file: IO[bytes] | None = None

    def __enter__(self) -> UploadLock:  # noqa: PYI034 -- Python 3.10 support
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self._file.tell() == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            raise UploadBusy("another transcript upload is already running")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._file is not None:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def upload_artifacts(
    s3: S3UploadClient,
    artifacts: list[ArchiveArtifact],
    on_progress: Callable[[int], None] | None = None,
) -> tuple[list[UploadResult], list[UploadError]]:
    """Upload redacted archives produced when the upload was started."""
    results = []
    errors = []

    def upload_one(artifact: ArchiveArtifact) -> UploadResult:
        metadata = {
            MTS_FORMAT_VERSION_METADATA: str(TRANSCRIPT_FORMAT_VERSION),
            MTS_TRANSCRIPT_ID_METADATA: artifact["transcript_id"],
            MTS_TRANSCRIPT_KIND_METADATA: artifact["transcript_kind"],
            MTS_SOURCE_METADATA: artifact["source"],
            MTS_COLLECTION_TYPE_METADATA: "contributed_project",
            CONTENT_SHA256_METADATA: artifact["content_sha256"],
            SOURCE_HASH_METADATA: artifact["source_hash"],
            SOURCE_HASH_VERSION_METADATA: str(SOURCE_HASH_VERSION),
            REDACTION_VERSION_METADATA: str(REDACTION_VERSION),
            FORMAT_VERSION_METADATA: str(TRANSCRIPT_FORMAT_VERSION),
        }
        if artifact["parent_transcript_id"]:
            metadata[MTS_PARENT_TRANSCRIPT_ID_METADATA] = artifact[
                "parent_transcript_id"
            ]
        if artifact["parent_object_key"]:
            metadata[MTS_PARENT_OBJECT_KEY_METADATA] = artifact[
                "parent_object_key"
            ]
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=artifact["key"],
            Body=Path(artifact["path"]).read_bytes(),
            ContentType="application/zip",
            Metadata=metadata,
        )
        return {
            key: artifact[key]
            for key in (
                "source", "project", "session", "parent", "zip_size_bytes",
                "redactions", "sidecar_count",
            )
        } | {"s3_key": artifact["key"], "transcript_count": 1}

    workers = min(upload_concurrency(), len(artifacts))
    if not workers:
        return results, errors
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(upload_one, item): item for item in artifacts}
        for future in as_completed(futures):
            artifact = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append({
                    "source": artifact.get("source", ""),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            if on_progress:
                on_progress(1)
    return results, errors
