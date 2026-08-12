"""Upload one overwrite-in-place ZIP per transcript using ``mts-trans``."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

from .paths import upload_lock_path
from .redactor import (
    redact_identity,
    redact_jsonl_content,
    redact_path_token,
)
from .s3client import S3_BUCKET
from .scan import load_transcript_inputs
from .sidecars import EMPTY as NO_SIDECARS
from .sidecars import SidecarSet
from .storage import STORAGE_PREFIX

TRANSCRIPT_FORMAT_VERSION = 4
SOURCE_HASH_VERSION = 3
# Increment this whenever redaction behavior or policy changes so unchanged
# source content is redacted and uploaded again under the new policy.
REDACTION_VERSION = 1
SOURCE_HASH_METADATA = "source-hash"
TRANSCRIPT_HASH_METADATA = "transcript-hash"
SIDECAR_COUNT_METADATA = "sidecar-count"
SOURCE_HASH_VERSION_METADATA = "source-hash-version"
REDACTION_VERSION_METADATA = "redaction-version"
FORMAT_VERSION_METADATA = "transcript-format-version"

# Read-only compatibility with uploads written before the terminology was
# clarified. New uploads use the *_HASH_* names above.
LEGACY_SOURCE_HASH_METADATA = "content-fingerprint"
LEGACY_TRANSCRIPT_HASH_METADATA = "body-fingerprint"
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


@dataclass(frozen=True)
class PreparedTranscript:
    session: object
    raw_bytes: bytes
    transcript_hash: str
    source_hash: str
    key: str
    sidecars: SidecarSet = NO_SIDECARS
    filesystem_snapshot: list[dict] | None = None
    sidecar_count: int | None = None


def transcript_hash(raw_bytes: bytes) -> str:
    """Identify the exact original content without running redaction."""
    digest = hashlib.sha256(f"source-v{SOURCE_HASH_VERSION}\0".encode())
    digest.update(raw_bytes)
    return digest.hexdigest()


def source_hash(transcript_hash_value: str, sidecars: SidecarSet) -> str:
    """Extend a transcript hash to cover its side files.

    A session without side files keeps the transcript hash, so
    collecting side files does not force every earlier upload to be rewritten.
    """
    if not sidecars.files:
        return transcript_hash_value
    digest = hashlib.sha256(f"sidecars-v1\0{transcript_hash_value}".encode())
    for sidecar in sidecars.files:
        try:
            raw_bytes = sidecar.path.read_bytes()
        except OSError:
            raw_bytes = b""
        digest.update(sidecar.arcname.encode() + b"\0")
        digest.update(hashlib.sha256(raw_bytes).digest())
        digest.update(b"\0")
    return digest.hexdigest()


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
    remote_source_hash = remote.get(
        SOURCE_HASH_METADATA, remote.get(LEGACY_SOURCE_HASH_METADATA)
    )
    if remote_source_hash == prepared.source_hash:
        return True
    # Harnesses delete their own side files on their own schedule, so a session
    # can lose them while its transcript stays put. Leave the fuller upload
    # alone rather than overwriting it with one that has less in it.
    try:
        uploaded_count = int(remote.get(SIDECAR_COUNT_METADATA, "0"))
    except ValueError:
        return False
    return (
        remote.get(
            TRANSCRIPT_HASH_METADATA,
            remote.get(LEGACY_TRANSCRIPT_HASH_METADATA),
        ) == prepared.transcript_hash
        and (
            prepared.sidecar_count
            if prepared.sidecar_count is not None
            else len(prepared.sidecars.files)
        ) < uploaded_count
    )


def prepare_transcript(source, session, contributor: str) -> PreparedTranscript:
    """Read and hash one stable transcript/sidecar snapshot."""
    path = Path(session.path)
    key = transcript_key(contributor, source.id, session)
    for _attempt in range(2):
        transcript_before = _path_signature(path)
        inputs = load_transcript_inputs(source, session)
        raw_bytes = inputs.raw_bytes
        if transcript_before != _path_signature(path):
            continue
        transcript_hash_value = transcript_hash(raw_bytes)
        sidecars = inputs.sidecars
        probe = PreparedTranscript(
            session=session,
            raw_bytes=raw_bytes,
            transcript_hash=transcript_hash_value,
            source_hash="",
            key=key,
            sidecars=sidecars,
        )
        snapshot = filesystem_snapshot(probe)
        prepared = PreparedTranscript(
            session=session,
            raw_bytes=raw_bytes,
            transcript_hash=transcript_hash_value,
            source_hash=source_hash(transcript_hash_value, sidecars),
            key=key,
            sidecars=sidecars,
            filesystem_snapshot=snapshot,
            sidecar_count=len(sidecars.files),
        )
        if snapshot == filesystem_snapshot(prepared):
            return prepared
    raise RuntimeError("Transcript changed while it was being hashed")


def classify_prepared(
    s3,
    prepared_items: list[PreparedTranscript],
    uploaded_metadata: dict[str, dict] | None = None,
    on_status=None,
):
    """Split hashed transcripts by their remote source hash."""
    existing = uploaded_metadata if uploaded_metadata is not None else {}
    pending = []
    current = []
    errors = []

    def check_one(prepared):
        remote = _uploaded_metadata(s3, prepared.key, existing)
        return _already_uploaded(prepared, remote)

    if not prepared_items:
        return pending, current, errors
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
                is_current = future.result()
            except Exception as exc:
                errors.append({
                    "session": prepared.session.id,
                    "key": prepared.key,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                (current if is_current else pending).append(prepared)
            if on_status:
                on_status(completed, len(prepared_items))
    return pending, current, errors


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
    project_id, project_label = session.project_id, session.project_label
    sidecar_contents, sidecar_section, count = _redacted_sidecars(prepared)
    redaction_count += count
    project_id, count = redact_path_token(project_id)
    redaction_count += count
    project_label, count = redact_identity(project_label)
    redaction_count += count

    suffix = Path(session.path).suffix.lower()
    if suffix not in {".jsonl", ".txt"}:
        suffix = ".txt"
    manifest = {
        "transcript_format_version": TRANSCRIPT_FORMAT_VERSION,
        "source": source.id,
        "source_format": source.source_format,
        "contributor": contributor,
        "project": {"key": project_id, "name": project_label},
        "session": {
            "id": session.id,
            "is_subagent": session.is_subagent,
            "parent": session.parent,
        },
        "version": {
            "source_hash": prepared.source_hash,
            "transcript_hash": prepared.transcript_hash,
            "source_hash_version": SOURCE_HASH_VERSION,
            "redaction_version": REDACTION_VERSION,
            "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "redact_identity": True,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        "size_bytes": len(raw.encode("utf-8")),
        "redactions": redaction_count,
        "sidecars": sidecar_section,
    }
    buffer = io.BytesIO()
    # These archives are transient upload payloads. Fast compression keeps one
    # large transcript from making readiness appear stalled for minutes.
    with zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1
    ) as archive:
        archive.writestr(f"transcript{suffix}", raw)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for arcname, text in sidecar_contents:
            archive.writestr(arcname, text)
    return buffer.getvalue(), manifest


def _path_signature(path: str | Path) -> dict:
    value = {"path": str(path)}
    try:
        stat = Path(path).stat()
    except OSError:
        value["exists"] = False
    else:
        value.update(exists=True, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    return value


def filesystem_snapshot(prepared: PreparedTranscript) -> list[dict]:
    """Capture every local path which can affect a prepared archive."""
    paths: list[str | Path] = [prepared.session.path]
    paths.extend(sidecar.path for sidecar in prepared.sidecars.files)
    paths.extend(prepared.sidecars.missing)
    paths.extend(prepared.sidecars.skipped)
    paths.extend(prepared.sidecars.directories)
    return [_path_signature(path) for path in dict.fromkeys(paths)]


def filesystem_snapshot_is_current(snapshot: object) -> bool:
    return isinstance(snapshot, list) and all(
        isinstance(item, dict)
        and item == _path_signature(str(item.get("path", "")))
        for item in snapshot
    )


def build_upload_artifact(
    source, prepared: PreparedTranscript, contributor: str, directory: str | Path
) -> dict:
    """Build a temporary redacted archive for a confirmed pending item."""
    archive, manifest = _build_transcript_zip(source, prepared, contributor)
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="transcript-", suffix=".zip", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(archive)
        os.chmod(temporary, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    session = prepared.session
    return {
        "source": source.id,
        "project": session.project_id,
        "session": session.id,
        "parent": session.parent,
        "path": temporary,
        "key": prepared.key,
        "source_hash": prepared.source_hash,
        "transcript_hash": prepared.transcript_hash,
        "sidecar_count": len(manifest["sidecars"]["files"]),
        "redactions": manifest["redactions"],
        "zip_size_bytes": len(archive),
        "filesystem_snapshot": filesystem_snapshot(prepared),
    }


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
                TRANSCRIPT_HASH_METADATA: artifact["transcript_hash"],
                SIDECAR_COUNT_METADATA: str(artifact["sidecar_count"]),
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
