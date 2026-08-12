"""Upload one overwrite-in-place ZIP per transcript using ``mts-trans``."""

from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from botocore.exceptions import ClientError

from .paths import upload_lock_path
from .prepare_archive import (
    REDACTION_VERSION,
    SOURCE_HASH_VERSION,
    TRANSCRIPT_FORMAT_VERSION,
    PreparedTranscript,
)
from .s3client import S3_BUCKET
from .storage import STORAGE_PREFIX

SOURCE_HASH_METADATA = "source-hash"
SOURCE_HASH_VERSION_METADATA = "source-hash-version"
REDACTION_VERSION_METADATA = "redaction-version"
FORMAT_VERSION_METADATA = "transcript-format-version"

# Read-only compatibility with uploads written before the terminology was
# clarified. New uploads use the *_HASH_* names above.
LEGACY_SOURCE_HASH_METADATA = "content-fingerprint"
LEGACY_SOURCE_HASH_VERSION_METADATA = "fingerprint-version"


def upload_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_UPLOAD_CONCURRENCY", "4")))


def metadata_concurrency() -> int:
    """Use more workers for small, read-only S3 metadata requests."""
    return max(1, int(os.environ.get("CTC_METADATA_CONCURRENCY", "16")))


class UploadBusy(RuntimeError):
    """Raised when another collector process owns the upload lock."""


class UploadLock:
    """Cross-process advisory lock covering manual and scheduled uploads."""

    def __init__(self, path: Path | None = None):
        self.path = path or upload_lock_path()
        self._file = None

    def __enter__(self):
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

    def __exit__(self, exc_type, exc, tb):
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


def _safe_segment(value: str) -> str:
    value = value.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-") or "unknown"
    if cleaned != value:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}--{suffix}"
    return cleaned


def _project_segment(session) -> str:
    value = session.project_label
    if not value:
        return "%00"
    return value.replace("%", "%25").replace("/", "%2F").replace("\\", "%5C")


def transcript_prefix(contributor: str, source_id: str, session) -> str:
    root = (
        f"{STORAGE_PREFIX}/{_safe_segment(contributor)}/"
        f"{_project_segment(session)}/{_safe_segment(source_id)}/"
    )
    if session.is_subagent and session.parent:
        return (
            f"{root}{_safe_segment(session.parent)}/subagents/"
            f"{_safe_segment(session.id)}/"
        )
    return f"{root}{_safe_segment(session.id)}/"


def transcript_key(contributor: str, source_id: str, session) -> str:
    return f"{transcript_prefix(contributor, source_id, session)}transcript.zip"


def _uploaded_metadata(
    s3, key: str, cache: dict[str, tuple[bool, dict]]
) -> tuple[bool, dict]:
    if key in cache:
        return cache[key]
    try:
        response = s3.head_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
        result = (False, {})
    else:
        result = (True, response.get("Metadata", {}))
    cache[key] = result
    return result


def _already_uploaded(prepared: PreparedTranscript, remote: dict) -> bool:
    """Decide whether the stored object already holds this session's content."""
    source_hash_version = remote.get(
        SOURCE_HASH_VERSION_METADATA,
        remote.get(LEGACY_SOURCE_HASH_VERSION_METADATA),
    )
    if (
        source_hash_version != str(SOURCE_HASH_VERSION)
        or remote.get(REDACTION_VERSION_METADATA) != str(REDACTION_VERSION)
        or remote.get(FORMAT_VERSION_METADATA) != str(TRANSCRIPT_FORMAT_VERSION)
    ):
        return False
    return remote.get(
        SOURCE_HASH_METADATA, remote.get(LEGACY_SOURCE_HASH_METADATA)
    ) == prepared.source_hash


def classify_prepared(
    s3,
    prepared_items: list[PreparedTranscript],
    uploaded_metadata: dict[str, tuple[bool, dict]] | None = None,
    on_status=None,
):
    """Split transcripts into absent, changed, and current S3 objects."""
    existing = uploaded_metadata if uploaded_metadata is not None else {}
    not_uploaded = []
    changed = []
    current = []
    errors = []

    def check_one(prepared):
        exists, remote = _uploaded_metadata(s3, prepared.key, existing)
        if not exists:
            return "not_uploaded"
        return "current" if _already_uploaded(prepared, remote) else "changed"

    if not prepared_items:
        return not_uploaded, changed, current, errors
    workers = min(metadata_concurrency(), len(prepared_items))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(check_one, item): item for item in prepared_items
        }
        for future in as_completed(futures):
            prepared = futures[future]
            completed += 1
            try:
                state = future.result()
            except Exception as exc:
                errors.append({
                    "session": prepared.session.id,
                    "key": prepared.key,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                {
                    "not_uploaded": not_uploaded,
                    "changed": changed,
                    "current": current,
                }[state].append(prepared)
            if on_status:
                on_status(completed, len(prepared_items))
    return not_uploaded, changed, current, errors


def upload_artifacts(s3, artifacts: list[dict], on_progress=None):
    """Upload redacted archives produced when the upload was started."""
    results = []
    errors = []

    def upload_one(artifact):
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=artifact["key"],
            Body=Path(artifact["path"]).read_bytes(),
            ContentType="application/zip",
            Metadata={
                SOURCE_HASH_METADATA: artifact["source_hash"],
                SOURCE_HASH_VERSION_METADATA: str(SOURCE_HASH_VERSION),
                REDACTION_VERSION_METADATA: str(REDACTION_VERSION),
                FORMAT_VERSION_METADATA: str(TRANSCRIPT_FORMAT_VERSION),
            },
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
