"""Persistent scan-to-upload state shared by the Web UI and hourly watcher.

The pipeline has one durable record per local transcript and contributor. A
cheap filesystem snapshot decides whether hashing is necessary. New or
changed records are read, hashed, and reconciled with S3 without running
redaction. Uploads redact and package only the confirmed pending records.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .cache import (
    CacheRecord,
    cache_record_key,
    get_cache,
    get_cache_for_transcript,
    save_cache,
    set_cache_for_transcript,
)
from .s3client import make_s3_client
from .uploader import (
    REDACTION_VERSION,
    SOURCE_HASH_VERSION,
    TRANSCRIPT_FORMAT_VERSION,
    PreparedTranscript,
    build_upload_artifact,
    classify_prepared,
    filesystem_snapshot,
    filesystem_snapshot_is_current,
    prepare_transcript,
)


def redaction_concurrency() -> int:
    """Return the number of transcripts prepared in parallel."""
    return max(1, int(os.environ.get("CTC_REDACTION_CONCURRENCY", "16")))


def _item(source_id: str, session, status: str) -> dict:
    return {
        "source": source_id,
        "group": session.group_key,
        "parent": session.parent,
        "session": session.id,
        "state": status,
    }


def _record_is_current(record: CacheRecord) -> bool:
    if (
        record.get("source_hash_version") != SOURCE_HASH_VERSION
        or record.get("redaction_version") != REDACTION_VERSION
        or record.get("format_version") != TRANSCRIPT_FORMAT_VERSION
        or not filesystem_snapshot_is_current(record.get("filesystem_snapshot"))
    ):
        return False
    return True


def _prepare_changed(source, session, contributor: str) -> tuple:
    """Hash one changed transcript without touching shared state."""
    prepared = prepare_transcript(source, session, contributor)
    record = {
        "source": source.id,
        "contributor": contributor,
        "group": session.group_key,
        "parent": session.parent,
        "session": session.id,
        "source_hash_version": SOURCE_HASH_VERSION,
        "redaction_version": REDACTION_VERSION,
        "format_version": TRANSCRIPT_FORMAT_VERSION,
        "filesystem_snapshot": prepared.filesystem_snapshot,
        "source_hash": prepared.source_hash,
        "transcript_hash": prepared.transcript_hash,
        "key": prepared.key,
        "sidecar_count": prepared.sidecar_count,
        "state": "checking",
    }
    # Remote comparison only needs hashes, sidecar count, and identity.
    # Release transcript bodies as each worker finishes instead of retaining a
    # cold cache's entire corpus until every S3 metadata check completes.
    return replace(prepared, raw_bytes=b""), record


def _prepared_from_record(record: CacheRecord, session) -> PreparedTranscript | None:
    try:
        return PreparedTranscript(
            session=session,
            raw_bytes=b"",
            transcript_hash=str(record["transcript_hash"]),
            source_hash=str(record["source_hash"]),
            key=str(record["key"]),
            filesystem_snapshot=record["filesystem_snapshot"],
            sidecar_count=int(record.get("sidecar_count", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def refresh(
    selections,
    contributor: str,
    *,
    s3=None,
    on_progress=None,
    cache_path: Path | None = None,
) -> dict:
    """Hash changed transcripts and reconcile only those with S3."""
    selections = [(source, list(sessions)) for source, sessions in selections]
    all_sessions = [
        (source, session)
        for source, sessions in selections
        for session in sessions
    ]
    cache = get_cache(cache_path)
    records = cache["records"]
    changed = []
    prepared_by_key = {}
    items_by_key = {}

    for source, session in all_sessions:
        key = cache_record_key(contributor, source.id, session)
        record = get_cache_for_transcript(cache, contributor, source.id, session)
        status = record.get("state") if record is not None else None
        if record is not None and status == "current" and _record_is_current(record):
            items_by_key[key] = _item(source.id, session, "current")
        elif (
            record is not None
            and status == "ready"
            and _record_is_current(record)
            and (cached := _prepared_from_record(record, session)) is not None
        ):
            prepared_by_key[key] = cached
        else:
            changed.append((key, source, session))

    total_changed = len(changed)
    if on_progress:
        on_progress("hashing", 0, total_changed)
    errors = []
    workers = max(1, min(redaction_concurrency(), total_changed))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for key, source, session in changed:
            future = executor.submit(
                _prepare_changed, source, session, contributor
            )
            futures[future] = (key, source, session)
        for future in as_completed(futures):
            key, source, session = futures[future]
            completed += 1
            try:
                prepared, record = future.result()
            except Exception as exc:
                set_cache_for_transcript(
                    cache,
                    contributor,
                    source.id,
                    session,
                    {
                        "source": source.id,
                        "contributor": contributor,
                        "group": session.group_key,
                        "parent": session.parent,
                        "session": session.id,
                        "state": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                items_by_key[key] = _item(source.id, session, "error")
                errors.append({"source": source.id, "session": session.id,
                               "error": f"{type(exc).__name__}: {exc}"})
            else:
                set_cache_for_transcript(
                    cache, contributor, source.id, session, record
                )
                prepared_by_key[key] = prepared
            if on_progress:
                on_progress("hashing", completed, total_changed)
    save_cache(cache, cache_path)

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
            items_by_key[key] = _item(
                record["source"], prepared.session, record["state"]
            )

    save_cache(cache, cache_path)
    items = []
    for source, session in all_sessions:
        key = cache_record_key(contributor, source.id, session)
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
    """Return pending upload candidates or sessions requiring Refresh."""
    cache = get_cache(cache_path)
    candidates = []
    stale = []
    for source, sessions in selections:
        for session in sessions:
            record = get_cache_for_transcript(
                cache, contributor, source.id, session
            )
            if (
                record is not None
                and record.get("state") == "current"
                and _record_is_current(record)
            ):
                continue
            if (
                record is not None
                and record.get("state") == "ready"
                and _record_is_current(record)
            ):
                candidates.append(dict(record))
            else:
                stale.append(_item(source.id, session, "stale"))
    return candidates, stale


def prepare_upload_artifacts(
    selections,
    candidates: list[dict],
    contributor: str,
    directory: str | Path,
    on_progress=None,
):
    """Revalidate, redact, and package candidates selected for upload."""
    wanted = {
        (item.get("source"), item.get("group"), item.get("parent") or "",
         item.get("session")): item
        for item in candidates
    }
    work = []
    resolved = set()
    for source, sessions in selections:
        for session in sessions:
            identity = (source.id, session.group_key, session.parent or "", session.id)
            if identity in wanted and identity not in resolved:
                work.append((source, session, wanted[identity]))
                resolved.add(identity)

    artifacts = []
    errors = [
        {
            "source": str(identity[0] or ""),
            "session": str(identity[3] or ""),
            "error": "Upload candidate is no longer available; refresh and try again",
        }
        for identity in wanted
        if identity not in resolved
    ]

    def prepare_one(source, session, candidate):
        prepared = prepare_transcript(source, session, contributor)
        if (
            prepared.source_hash != candidate.get("source_hash")
            or prepared.transcript_hash != candidate.get("transcript_hash")
            or prepared.filesystem_snapshot != candidate.get("filesystem_snapshot")
        ):
            raise RuntimeError("Transcript changed after Refresh; refresh and try again")
        artifact = build_upload_artifact(
            source, prepared, contributor, directory
        )
        if filesystem_snapshot(prepared) != candidate.get("filesystem_snapshot"):
            Path(artifact["path"]).unlink(missing_ok=True)
            raise RuntimeError("Transcript changed during redaction; refresh and try again")
        return artifact

    total = len(wanted)
    workers = max(1, min(redaction_concurrency(), len(work)))
    completed = len(errors)
    if on_progress and completed:
        on_progress(completed, total)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(prepare_one, source, session, candidate):
                (source, session)
            for source, session, candidate in work
        }
        for future in as_completed(futures):
            source, session = futures[future]
            completed += 1
            try:
                artifacts.append(future.result())
            except Exception as exc:
                errors.append({
                    "source": source.id,
                    "session": session.id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            if on_progress:
                on_progress(completed, total)
    return artifacts, errors


def mark_uploaded(artifacts: list[dict], contributor: str,
                  cache_path: Path | None = None) -> None:
    cache = get_cache(cache_path)
    records = cache["records"]
    uploaded = {
        (item.get("source"), item.get("group"), item.get("parent") or "",
         item.get("session"))
        for item in artifacts
    }
    for record in records.values():
        if record.get("contributor") != contributor:
            continue
        identity = (
            record.get("source"), record.get("group"), record.get("parent") or "",
            record.get("session"),
        )
        if identity in uploaded:
            record["state"] = "current"
            record.pop("error", None)
    save_cache(cache, cache_path)
