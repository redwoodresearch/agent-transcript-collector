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
from .sidecars import EMPTY as NO_SIDECARS
from .sidecars import SidecarSet
from .sources.base import session_sidecars
from .storage import STORAGE_PREFIX

TRANSCRIPT_FORMAT_VERSION = 4
FINGERPRINT_VERSION = 2
FINGERPRINT_METADATA = "content-fingerprint"
BODY_FINGERPRINT_METADATA = "body-fingerprint"
SIDECAR_COUNT_METADATA = "sidecar-count"
_FINGERPRINT_CACHE_MAX = 4096
_FINGERPRINT_CACHE: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_FINGERPRINT_CACHE_LOCK = threading.Lock()


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


@dataclass(frozen=True)
class PreparedTranscript:
    session: object
    raw_bytes: bytes
    body_fingerprint: str
    fingerprint: str
    key: str
    sidecars: SidecarSet = NO_SIDECARS


def _privacy_safe_digest(text: str) -> str:
    canonical, _ = redact_identity(canonicalize_secrets(text))
    return hashlib.sha256(canonical.encode()).hexdigest()


def transcript_fingerprint(raw_bytes: bytes) -> str:
    """Identify content without preserving or hashing raw secret values."""
    policy = f"archive-v{FINGERPRINT_VERSION}:redact-id=1\0"
    canonical = canonicalize_secrets(raw_bytes.decode("utf-8", errors="replace"))
    canonical, _ = redact_identity(canonical)
    return hashlib.sha256(policy.encode() + canonical.encode()).hexdigest()


def content_fingerprint(body_fingerprint: str, sidecars: SidecarSet) -> str:
    """Extend a transcript fingerprint to cover its side files.

    A session without side files keeps the fingerprint it always had, so
    collecting side files does not force every earlier upload to be rewritten.
    """
    if not sidecars.files:
        return body_fingerprint
    digest = hashlib.sha256(f"sidecars-v1\0{body_fingerprint}".encode())
    for sidecar in sidecars.files:
        try:
            text = sidecar.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        digest.update(f"{sidecar.arcname}\0{_privacy_safe_digest(text)}\0".encode())
    return digest.hexdigest()


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


def _uploaded_metadata(s3, key: str, cache: dict[str, dict]) -> dict:
    if key in cache:
        return cache[key]
    try:
        response = s3.head_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
        metadata = {}
    else:
        metadata = response.get("Metadata", {})
    cache[key] = metadata
    return metadata


def _already_uploaded(prepared: PreparedTranscript, remote: dict) -> bool:
    """Decide whether the stored object already holds this session's content."""
    if remote.get(FINGERPRINT_METADATA) == prepared.fingerprint:
        return True
    # Harnesses delete their own side files on their own schedule, so a session
    # can lose them while its transcript stays put. Leave the fuller upload
    # alone rather than overwriting it with one that has less in it.
    try:
        uploaded_count = int(remote.get(SIDECAR_COUNT_METADATA, "0"))
    except ValueError:
        return False
    return (
        remote.get(BODY_FINGERPRINT_METADATA) == prepared.body_fingerprint
        and len(prepared.sidecars.files) < uploaded_count
    )


def _prepare(source, session, contributor: str) -> PreparedTranscript:
    path = Path(session.path)
    stat = path.stat()
    cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    raw_bytes = path.read_bytes()
    with _FINGERPRINT_CACHE_LOCK:
        body_fingerprint = _FINGERPRINT_CACHE.get(cache_key)
        if body_fingerprint is not None:
            _FINGERPRINT_CACHE.move_to_end(cache_key)
    if body_fingerprint is None:
        body_fingerprint = transcript_fingerprint(raw_bytes)
        with _FINGERPRINT_CACHE_LOCK:
            _FINGERPRINT_CACHE[cache_key] = body_fingerprint
            _FINGERPRINT_CACHE.move_to_end(cache_key)
            while len(_FINGERPRINT_CACHE) > _FINGERPRINT_CACHE_MAX:
                _FINGERPRINT_CACHE.popitem(last=False)
    sidecars = session_sidecars(
        source, session, raw_bytes.decode("utf-8", errors="replace")
    )
    key = transcript_key(contributor, source.id, session)
    return PreparedTranscript(
        session,
        raw_bytes,
        body_fingerprint,
        content_fingerprint(body_fingerprint, sidecars),
        key,
        sidecars,
    )


def partition_transcripts(
    s3,
    source,
    sessions,
    contributor: str,
    uploaded_metadata: dict[str, dict] | None = None,
    on_status=None,
):
    """Split current transcripts into changed and already uploaded."""
    existing = uploaded_metadata if uploaded_metadata is not None else {}
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
        remote = _uploaded_metadata(s3, prepared.key, existing)
        return _already_uploaded(prepared, remote)

    if prepared_items:
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


def _redact(text: str) -> tuple[str, int]:
    text, redaction_count = redact_jsonl_content(text)
    text, count = redact_identity(text)
    return text, redaction_count + count


def _redacted_sidecars(
    prepared: PreparedTranscript,
) -> tuple[list[tuple[str, str]], dict, int]:
    """Return (arcname, text) pairs to archive plus the manifest section."""
    contents: list[tuple[str, str]] = []
    entries = []
    missing = list(prepared.sidecars.missing)
    redaction_count = 0
    for sidecar in prepared.sidecars.files:
        try:
            text = sidecar.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            missing.append(sidecar.reference)
            continue
        text, count = _redact(text)
        redaction_count += count
        contents.append((sidecar.arcname, text))
        entries.append({
            "path": sidecar.arcname,
            "kind": sidecar.kind,
            # The transcript names side files by absolute path, so the redacted
            # form of that path is what joins a pointer to its archived file.
            "referenced_as": redact_identity(sidecar.reference)[0],
            "size_bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
    section = {
        "files": entries,
        "missing": [redact_identity(path)[0] for path in sorted(missing)],
        "skipped_too_large": [
            redact_identity(path)[0] for path in prepared.sidecars.skipped
        ],
    }
    return contents, section, redaction_count


def _build_transcript_zip(source, prepared: PreparedTranscript, contributor: str):
    session = prepared.session
    raw, redaction_count = _redact(prepared.raw_bytes.decode("utf-8", errors="replace"))
    group_key, group_label = session.group_key, session.group_label
    sidecar_contents, sidecar_section, count = _redacted_sidecars(prepared)
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
            "body_fingerprint": prepared.body_fingerprint,
            "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "redact_identity": True,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        "size_bytes": len(raw.encode("utf-8")),
        "redactions": redaction_count,
        "sidecars": sidecar_section,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"transcript{suffix}", raw)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for arcname, text in sidecar_contents:
            archive.writestr(arcname, text)
    return buffer.getvalue(), manifest


def upload_transcripts(
    s3,
    source,
    sessions,
    contributor: str,
    on_progress=None,
    on_status=None,
    uploaded_metadata: dict[str, dict] | None = None,
):
    """Overwrite each session object only when its redacted content changes."""
    existing = uploaded_metadata if uploaded_metadata is not None else {}
    pending, uploaded, errors = partition_transcripts(
        s3, source, list(sessions), contributor, existing, on_status=on_status
    )
    if on_progress:
        on_progress(len(uploaded) + len(errors))
    if not pending:
        return [], errors

    def upload_one(prepared: PreparedTranscript):
        zip_bytes, manifest = _build_transcript_zip(source, prepared, contributor)
        sidecar_count = len(manifest["sidecars"]["files"])
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=prepared.key,
            Body=zip_bytes,
            ContentType="application/zip",
            Metadata={
                FINGERPRINT_METADATA: prepared.fingerprint,
                BODY_FINGERPRINT_METADATA: prepared.body_fingerprint,
                SIDECAR_COUNT_METADATA: str(sidecar_count),
            },
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
            "sidecar_count": sidecar_count,
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
                existing[prepared.key] = {
                    FINGERPRINT_METADATA: prepared.fingerprint,
                    BODY_FINGERPRINT_METADATA: prepared.body_fingerprint,
                    SIDECAR_COUNT_METADATA: str(result["sidecar_count"]),
                }
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
