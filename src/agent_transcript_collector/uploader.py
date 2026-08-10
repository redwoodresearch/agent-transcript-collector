"""Upload one overwrite-in-place ZIP per transcript using ``mts-trans``."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

from .paths import upload_lock_path
from .redactor import (
    canonicalize_secrets,
    local_usernames,
    redact_identity,
    redact_jsonl_content,
    redact_path_token,
)
from .s3client import S3_BUCKET
from .storage import STORAGE_PREFIX

TRANSCRIPT_FORMAT_VERSION = 3
FINGERPRINT_VERSION = 2
FINGERPRINT_METADATA = "content-fingerprint"
_FINGERPRINT_CACHE_MAX = 4096
_FINGERPRINT_CACHE: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_FINGERPRINT_CACHE_LOCK = threading.Lock()


def upload_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_UPLOAD_CONCURRENCY", "4")))


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


@dataclass(frozen=True)
class PreparedTranscript:
    session: object
    raw_bytes: bytes
    fingerprint: str
    key: str


def transcript_fingerprint(raw_bytes: bytes) -> str:
    """Identify content without preserving or hashing raw secret values."""
    policy = f"archive-v{FINGERPRINT_VERSION}:redact-id=1\0"
    canonical = canonicalize_secrets(raw_bytes.decode("utf-8", errors="replace"))
    canonical, _ = redact_identity(canonical)
    return hashlib.sha256(policy.encode() + canonical.encode()).hexdigest()


def _safe_segment(value: str) -> str:
    value = value.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-") or "unknown"
    if cleaned != value:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}--{suffix}"
    return cleaned


def _project_segment(session) -> str:
    value = session.group_label
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


def _uploaded_fingerprint(s3, key: str, cache: dict[str, str | None]) -> str | None:
    if key in cache:
        return cache[key]
    try:
        response = s3.head_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
        fingerprint = None
    else:
        fingerprint = response.get("Metadata", {}).get(FINGERPRINT_METADATA)
    cache[key] = fingerprint
    return fingerprint


def _prepare(source, session, contributor: str) -> PreparedTranscript:
    path = Path(session.path)
    stat = path.stat()
    cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    raw_bytes = path.read_bytes()
    with _FINGERPRINT_CACHE_LOCK:
        fingerprint = _FINGERPRINT_CACHE.get(cache_key)
        if fingerprint is not None:
            _FINGERPRINT_CACHE.move_to_end(cache_key)
    if fingerprint is None:
        fingerprint = transcript_fingerprint(raw_bytes)
        with _FINGERPRINT_CACHE_LOCK:
            _FINGERPRINT_CACHE[cache_key] = fingerprint
            _FINGERPRINT_CACHE.move_to_end(cache_key)
            while len(_FINGERPRINT_CACHE) > _FINGERPRINT_CACHE_MAX:
                _FINGERPRINT_CACHE.popitem(last=False)
    key = transcript_key(contributor, source.id, session)
    return PreparedTranscript(session, raw_bytes, fingerprint, key)


def partition_transcripts(
    s3,
    source,
    sessions,
    contributor: str,
    uploaded_fingerprints: dict[str, str | None] | None = None,
    on_status=None,
):
    """Split current transcripts into changed and already uploaded."""
    existing = uploaded_fingerprints if uploaded_fingerprints is not None else {}
    sessions = list(sessions)
    pending: list[PreparedTranscript] = []
    uploaded = []
    errors = []
    prepared_items = []
    for index, session in enumerate(sessions, start=1):
        try:
            prepared_items.append(_prepare(source, session, contributor))
        except OSError as exc:
            errors.append({"source": source.id, "error": f"{session.id}: {exc}"})
        if on_status:
            on_status("fingerprinting", index, len(sessions))

    def check_one(prepared):
        return _uploaded_fingerprint(s3, prepared.key, existing) == prepared.fingerprint

    if prepared_items:
        workers = min(upload_concurrency(), len(prepared_items))
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(check_one, item): item for item in prepared_items
            }
            for future in as_completed(futures):
                prepared = futures[future]
                completed += 1
                try:
                    already_uploaded = future.result()
                except Exception as exc:
                    errors.append({
                        "source": source.id,
                        "error": f"{prepared.session.id}: {type(exc).__name__}: {exc}",
                    })
                else:
                    (uploaded if already_uploaded else pending).append(
                        prepared.session if already_uploaded else prepared)
                if on_status:
                    on_status("checking", completed, len(prepared_items))
    return pending, uploaded, errors


def _build_transcript_zip(source, prepared: PreparedTranscript, contributor: str):
    session = prepared.session
    raw = prepared.raw_bytes.decode("utf-8", errors="replace")
    raw, redaction_count = redact_jsonl_content(raw)
    group_key, group_label = session.group_key, session.group_label
    raw, count = redact_identity(raw)
    redaction_count += count
    group_key, count = redact_path_token(group_key)
    redaction_count += count
    group_label, count = redact_identity(group_label)
    redaction_count += count

    suffix = Path(session.path).suffix.lower()
    if suffix not in {".jsonl", ".txt"}:
        suffix = ".txt"
    manifest = {
        "transcript_format_version": TRANSCRIPT_FORMAT_VERSION,
        "source": source.id,
        "source_format": source.source_format,
        "contributor": contributor,
        "project": {"key": group_key, "name": group_label},
        "session": {
            "id": session.id,
            "is_subagent": session.is_subagent,
            "parent": session.parent,
        },
        "version": {
            "fingerprint": prepared.fingerprint,
            "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "redact_identity": True,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        "size_bytes": len(raw.encode("utf-8")),
        "redactions": redaction_count,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"transcript{suffix}", raw)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buffer.getvalue(), manifest


def upload_transcripts(
    s3,
    source,
    sessions,
    contributor: str,
    on_progress=None,
    on_status=None,
    uploaded_fingerprints: dict[str, str | None] | None = None,
):
    """Overwrite each session object only when its redacted content changes."""
    existing = uploaded_fingerprints if uploaded_fingerprints is not None else {}
    pending, uploaded, errors = partition_transcripts(
        s3, source, list(sessions), contributor, existing, on_status=on_status
    )
    if on_progress:
        on_progress(len(uploaded) + len(errors))
    if not pending:
        return [], errors

    def upload_one(prepared: PreparedTranscript):
        zip_bytes, manifest = _build_transcript_zip(source, prepared, contributor)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=prepared.key,
            Body=zip_bytes,
            ContentType="application/zip",
            Metadata={FINGERPRINT_METADATA: prepared.fingerprint},
        )
        return {
            "source": source.id,
            "group": prepared.session.group_key,
            "session": prepared.session.id,
            "parent": prepared.session.parent,
            "s3_key": prepared.key,
            "transcript_count": 1,
            "zip_size_bytes": len(zip_bytes),
            "redactions": manifest["redactions"],
        }

    local_usernames()
    results = []
    completed = 0
    if on_status:
        on_status("uploading", 0, len(pending))
    workers = min(upload_concurrency(), len(pending))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(upload_one, item): item for item in pending}
        for future in as_completed(futures):
            prepared = futures[future]
            try:
                result = future.result()
                results.append(result)
                existing[prepared.key] = prepared.fingerprint
            except Exception as exc:
                errors.append(
                    {"source": source.id, "error": f"{type(exc).__name__}: {exc}"}
                )
            if on_progress:
                on_progress(1)
            completed += 1
            if on_status:
                on_status("uploading", completed, len(pending))
    return results, errors
