"""Shared, version-aware transcript archive and receipt uploader."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .paths import upload_lock_path
from .redactor import (
    local_usernames,
    redact_identity,
    redact_jsonl_content,
    redact_path_token,
)
from .s3client import S3_BUCKET


UNIT_BYTES = int(os.environ.get("CTC_UNIT_BYTES", str(25 * 1024 * 1024)))
UPLOAD_CONCURRENCY = max(1, int(os.environ.get("CTC_UPLOAD_CONCURRENCY", "4")))
RECEIPT_DIR = "_uploaded"
ARCHIVE_FORMAT_VERSION = 1


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


def archive_fingerprint(raw_bytes: bytes, redact_id: bool) -> str:
    """Identify the source bytes and processing policy used for an archive."""
    policy = f"archive-v{ARCHIVE_FORMAT_VERSION}:redact-id={int(redact_id)}\0"
    return hashlib.sha256(policy.encode() + raw_bytes).hexdigest()


def _group_token(group_key: str) -> str:
    return "g" + hashlib.sha1(group_key.encode("utf-8")).hexdigest()[:12]


def _receipt_token(group: str, parent: str | None, session: str) -> str:
    identity = json.dumps([group, parent or "", session], separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _receipt_prefix(source_id: str, contributor: str) -> str:
    return f"{source_id}/{contributor}/{RECEIPT_DIR}/"


def _receipt_key(
    source_id: str, contributor: str, session, fingerprint: str
) -> str:
    identity = _receipt_token(session.group_key, session.parent, session.id)
    return f"{_receipt_prefix(source_id, contributor)}{identity}/{fingerprint}"


def list_receipt_versions(
    s3, source_id: str, contributor: str
) -> dict[str, set[str]]:
    """Return uploaded archive versions by opaque transcript identity."""
    prefix = _receipt_prefix(source_id, contributor)
    versions: dict[str, set[str]] = defaultdict(set)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            suffix = obj["Key"][len(prefix):]
            parts = suffix.split("/")
            if len(parts) == 2 and all(parts):
                versions[parts[0]].add(parts[1])
    return dict(versions)


def partition_uploaded(s3, source, sessions, contributor, redact_id=True):
    """Split sessions into pending and uploaded using the shared receipt policy."""
    versions = list_receipt_versions(s3, source.id, contributor)
    pending, uploaded, errors = [], [], []
    for session in sessions:
        try:
            raw_bytes = Path(session.path).read_bytes()
        except OSError as exc:
            errors.append(
                {"source": source.id, "error": f"{session.id}: {exc}"}
            )
            continue
        fingerprint = archive_fingerprint(raw_bytes, redact_id)
        identity = _receipt_token(session.group_key, session.parent, session.id)
        target = uploaded if fingerprint in versions.get(identity, set()) else pending
        target.append(session)
    return pending, uploaded, errors


def _plan_units(sessions, unit_bytes: int | None = None):
    """Split sessions into deterministic, size-budgeted group units."""
    budget = UNIT_BYTES if unit_bytes is None else unit_bytes
    by_group = defaultdict(list)
    for session in sessions:
        by_group[session.group_key].append(session)
    for group_key in sorted(by_group):
        members = sorted(
            by_group[group_key], key=lambda session: (session.parent or "", session.id)
        )
        part, current, current_bytes = 0, [], 0
        for session in members:
            size = session.size_bytes or 0
            if current and current_bytes + size > budget:
                yield group_key, part, current
                part, current, current_bytes = part + 1, [], 0
            current.append(session)
            current_bytes += size
        if current:
            yield group_key, part, current


def _members_hash(included: list[tuple[object, str]]) -> str:
    members = "\n".join(
        f"{session.parent or ''}/{session.id}/{fingerprint}"
        for session, fingerprint in included
    )
    return hashlib.sha1(members.encode("utf-8")).hexdigest()[:12]


def _unit_key(source, contributor, group_key, part, included) -> str:
    return (
        f"{source.id}/{contributor}/{_group_token(group_key)}/"
        f"part-{part:03d}-{_members_hash(included)}.zip"
    )


def _build_unit_zip(source, unit_sessions, contributor, redact_id=True):
    """Build an archive and return its manifest plus included archive versions."""
    buffer = io.BytesIO()
    manifest_sessions = []
    included: list[tuple[object, str]] = []
    total_redactions = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for session in unit_sessions:
            try:
                raw_bytes = Path(session.path).read_bytes()
            except OSError:
                continue
            content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            fingerprint = archive_fingerprint(raw_bytes, redact_id)
            raw = raw_bytes.decode("utf-8", errors="replace")
            included.append((session, fingerprint))
            raw, redaction_count = redact_jsonl_content(raw)
            group_key, group_label = session.group_key, session.group_label
            if redact_id:
                raw, count = redact_identity(raw)
                redaction_count += count
                group_key, count = redact_path_token(group_key)
                redaction_count += count
                group_label, count = redact_path_token(group_label)
                redaction_count += count
            total_redactions += redaction_count
            if session.is_subagent and session.parent:
                archive_path = (
                    f"{group_key}/{session.parent}/subagents/{session.id}.jsonl"
                )
            else:
                archive_path = f"{group_key}/{session.id}.jsonl"
            archive.writestr(archive_path, raw)
            manifest_sessions.append(
                {
                    "group": group_key,
                    "group_label": group_label,
                    "session": session.id,
                    "is_subagent": session.is_subagent,
                    "parent": session.parent,
                    "size_bytes": len(raw.encode("utf-8")),
                    "content_sha256": content_sha256,
                    "redactions": redaction_count,
                }
            )
        manifest = {
            "archive_format_version": ARCHIVE_FORMAT_VERSION,
            "source": source.id,
            "source_format": source.source_format,
            "contributor": contributor,
            "redact_identity": redact_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "subagent_count": sum(
                1 for session in manifest_sessions if session["is_subagent"]
            ),
            "sessions": manifest_sessions,
            "total_redactions": total_redactions,
        }
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buffer.getvalue(), manifest, included


def upload_units(
    s3,
    source,
    sessions,
    contributor,
    redact_id=True,
    on_progress=None,
    *,
    unit_bytes: int | None = None,
):
    """Upload sessions that do not already have matching receipts."""
    sessions = list(sessions)
    pending, already_uploaded, errors = partition_uploaded(
        s3, source, sessions, contributor, redact_id
    )
    if on_progress:
        on_progress(len(already_uploaded) + len(errors))
    units = list(_plan_units(pending, unit_bytes))
    uploaded = []
    if not units:
        return uploaded, errors

    def upload_one(unit_spec):
        group_key, part, unit = unit_spec
        zip_bytes, manifest, included = _build_unit_zip(
            source, unit, contributor, redact_id
        )
        key = _unit_key(source, contributor, group_key, part, included)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=zip_bytes,
            ContentType="application/zip",
        )
        for session, fingerprint in included:
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=_receipt_key(source.id, contributor, session, fingerprint),
                Body=key.encode("utf-8"),
                ContentType="text/plain",
            )
        return {
            "source": source.id,
            "s3_key": key,
            "session_count": len(included),
            "zip_size_bytes": len(zip_bytes),
            "total_redactions": manifest["total_redactions"],
        }

    local_usernames()
    workers = min(UPLOAD_CONCURRENCY, len(units))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(upload_one, unit): unit for unit in units}
        for future in as_completed(futures):
            count = len(futures[future][2])
            try:
                uploaded.append(future.result())
            except Exception as exc:
                errors.append(
                    {"source": source.id, "error": f"{type(exc).__name__}: {exc}"}
                )
            if on_progress:
                on_progress(count)
    return uploaded, errors
