"""Persistent scan-to-upload state shared by the Web UI and hourly watcher.

The pipeline has one durable record per local transcript and contributor. A
cheap filesystem signature decides whether redaction is necessary; only new or
changed records are read, fingerprinted, packaged, and reconciled with S3.
Uploads consume the resulting artifacts without repeating preparation.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .paths import pipeline_cache_path, prepared_artifacts_dir
from .s3client import make_s3_client
from .uploader import (
    FINGERPRINT_VERSION,
    TRANSCRIPT_FORMAT_VERSION,
    artifact_is_available,
    artifact_is_current,
    build_upload_artifact,
    classify_prepared,
    prepare_transcript,
    prepared_signature,
    signature_is_current,
)

CACHE_VERSION = 1


def _identity(source_id: str, session) -> str:
    return json.dumps(
        [source_id, str(session.path), session.id, session.parent or ""],
        separators=(",", ":"),
    )


def _record_key(contributor: str, source_id: str, session) -> str:
    return json.dumps(
        [contributor, _identity(source_id, session)],
        separators=(",", ":"),
    )


def _load(path: Path | None = None) -> dict:
    target = path or pipeline_cache_path()
    try:
        value = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        value = {}
    if value.get("cache_version") != CACHE_VERSION:
        return {"cache_version": CACHE_VERSION, "records": {}}
    records = value.get("records")
    if not isinstance(records, dict):
        value["records"] = {}
    return value


def _save(state: dict, path: Path | None = None) -> None:
    target = path or pipeline_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _item(source_id: str, session, status: str) -> dict:
    return {
        "source": source_id,
        "group": session.group_key,
        "parent": session.parent,
        "session": session.id,
        "state": status,
    }


def _artifact_root(contributor: str, root: Path | None = None) -> Path:
    base = root or prepared_artifacts_dir()
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in contributor)
    target = base / (safe or "anonymous")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


def _unlink_artifact(record: dict, root: Path) -> None:
    artifact = record.get("artifact")
    if not isinstance(artifact, dict):
        return
    try:
        path = Path(artifact["path"])
        if path.resolve().is_relative_to(root.resolve()):
            path.unlink(missing_ok=True)
    except (KeyError, OSError, RuntimeError):
        pass


def _record_is_current(record: object, require_artifact: bool) -> bool:
    if not isinstance(record, dict):
        return False
    if (
        record.get("fingerprint_version") != FINGERPRINT_VERSION
        or record.get("format_version") != TRANSCRIPT_FORMAT_VERSION
        or not signature_is_current(record.get("signature"))
    ):
        return False
    if require_artifact:
        return artifact_is_current(record.get("artifact", {}))
    return True


def refresh(
    selections,
    contributor: str,
    *,
    s3=None,
    on_progress=None,
    cache_path: Path | None = None,
    artifact_root: Path | None = None,
) -> dict:
    """Prepare changed transcripts and reconcile only those with S3."""
    selections = [(source, list(sessions)) for source, sessions in selections]
    all_sessions = [
        (source, session)
        for source, sessions in selections
        for session in sessions
    ]
    state = _load(cache_path)
    records = state["records"]
    root = _artifact_root(contributor, artifact_root)
    changed = []
    items_by_key = {}

    for source, session in all_sessions:
        key = _record_key(contributor, source.id, session)
        record = records.get(key)
        status = record.get("state") if isinstance(record, dict) else None
        require_artifact = status == "ready"
        if status in {"current", "ready"} and _record_is_current(
            record, require_artifact
        ):
            items_by_key[key] = _item(source.id, session, status)
        else:
            changed.append((
                key, source, session,
                record if isinstance(record, dict) else {},
            ))

    prepared_by_key = {}
    total_changed = len(changed)
    if on_progress:
        on_progress("redacting", 0, total_changed)
    errors = []
    for index, (key, source, session, old_record) in enumerate(changed, start=1):
        try:
            _unlink_artifact(old_record, root)
            prepared = prepare_transcript(source, session, contributor)
            record = {
                "source": source.id,
                "contributor": contributor,
                "group": session.group_key,
                "parent": session.parent,
                "session": session.id,
                "path": str(session.path),
                "fingerprint_version": FINGERPRINT_VERSION,
                "format_version": TRANSCRIPT_FORMAT_VERSION,
                "signature": prepared_signature(prepared),
                "fingerprint": prepared.fingerprint,
                "body_fingerprint": prepared.body_fingerprint,
                "artifact": build_upload_artifact(
                    source, prepared, contributor, root
                ),
                "state": "checking",
            }
            records[key] = record
            prepared_by_key[key] = prepared
        except Exception as exc:
            records[key] = {
                "source": source.id,
                "contributor": contributor,
                "group": session.group_key,
                "parent": session.parent,
                "session": session.id,
                "path": str(session.path),
                "state": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            items_by_key[key] = _item(source.id, session, "error")
            errors.append({"source": source.id, "session": session.id,
                           "error": f"{type(exc).__name__}: {exc}"})
        if on_progress:
            on_progress("redacting", index, total_changed)
    _save(state, cache_path)

    prepared_items = list(prepared_by_key.values())
    if prepared_items:
        client = s3 or make_s3_client()

        def checked(done, total):
            if on_progress:
                on_progress("checking", done, total)

        if on_progress:
            on_progress("checking", 0, len(prepared_items))
        pending, current, check_errors = classify_prepared(
            client, prepared_items, on_status=checked
        )
        pending_ids = {id(item) for item in pending}
        current_ids = {id(item) for item in current}
        errors.extend(check_errors)
        for key, prepared in prepared_by_key.items():
            record = records[key]
            if id(prepared) in current_ids:
                record["state"] = "current"
                _unlink_artifact(record, root)
                record.pop("artifact", None)
            elif id(prepared) in pending_ids:
                record["state"] = "ready"
            else:
                record["state"] = "error"
                record["error"] = next(
                    (item.get("error", "Upload status unavailable")
                     for item in check_errors
                     if item.get("key") == prepared.key),
                    "Upload status unavailable",
                )
                _unlink_artifact(record, root)
                record.pop("artifact", None)
            items_by_key[key] = _item(
                record["source"], prepared.session, record["state"]
            )

    _save(state, cache_path)
    items = []
    for source, session in all_sessions:
        key = _record_key(contributor, source.id, session)
        item = items_by_key.get(key)
        if item is not None:
            items.append(item)
    usable = any(item["state"] in {"current", "ready"} for item in items)
    return {
        "status": "partial" if errors and usable else "failed" if errors else "ready",
        "items": items,
        "errors": errors,
        "total": len(all_sessions),
        "changed": total_changed,
        "checked": len(prepared_items),
        "cached": len(all_sessions) - total_changed,
    }


def artifacts_for(selections, contributor: str, cache_path: Path | None = None):
    """Return current prepared artifacts or the sessions requiring Refresh."""
    state = _load(cache_path)
    artifacts = []
    stale = []
    for source, sessions in selections:
        for session in sessions:
            key = _record_key(contributor, source.id, session)
            record = state["records"].get(key)
            if (
                isinstance(record, dict)
                and record.get("state") == "current"
                and _record_is_current(record, False)
            ):
                continue
            if (
                isinstance(record, dict)
                and record.get("state") == "ready"
                # Upload means "send the prepared snapshot." A live session
                # may change immediately afterward; Refresh notices that from
                # the record signature and prepares its next snapshot.
                and artifact_is_available(record.get("artifact", {}))
            ):
                artifacts.append(dict(record["artifact"]))
            else:
                stale.append(_item(source.id, session, "stale"))
    return artifacts, stale


def mark_uploaded(artifacts: list[dict], contributor: str,
                  cache_path: Path | None = None,
                  artifact_root: Path | None = None) -> None:
    state = _load(cache_path)
    records = state["records"]
    root = _artifact_root(contributor, artifact_root)
    uploaded = {
        (item.get("source"), item.get("group"), item.get("parent") or "",
         item.get("session"))
        for item in artifacts
    }
    for key, record in records.items():
        record_contributor = record.get("contributor")
        if record_contributor is None:
            try:
                record_contributor = json.loads(key)[0]
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
        if record_contributor != contributor:
            continue
        identity = (
            record.get("source"), record.get("group"), record.get("parent") or "",
            record.get("session"),
        )
        if identity in uploaded:
            record["state"] = "current"
            _unlink_artifact(record, root)
            record.pop("artifact", None)
            record.pop("error", None)
    _save(state, cache_path)
