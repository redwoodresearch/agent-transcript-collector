"""Coordinate cached upload status, archive preparation, and upload completion."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeAlias, cast

from .cache import (
    CacheRecord,
    get_cache,
    get_cache_for_transcript,
    reusable_status,
    save_cache,
    store_status,
)
from .prepare_archive import (
    TRANSCRIPT_FORMAT_VERSION,
    ArchiveArtifact,
    prepare_archive,
)
from .redactor import REDACTION_VERSION
from .s3client import make_s3_client
from .sources.base import Session, Source
from .storage import transcript_key
from .transcript import TranscriptIdentity, TranscriptRef, TranscriptStatus
from .transcript_snapshot import SOURCE_HASH_VERSION
from .upload_status import S3HeadClient, classify_uploads

Selections: TypeAlias = Iterable[tuple[Source, Iterable[Session]]]
PipelineItem: TypeAlias = dict[str, str | None]
ProgressCallback: TypeAlias = Callable[[str, int, int], None]
PreparationCallback: TypeAlias = Callable[[int, int], None]


def preparation_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_REDACTION_CONCURRENCY", "16")))


def refresh(
    selections: Selections,
    contributor: str,
    *,
    s3: S3HeadClient | None = None,
    on_progress: ProgressCallback | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Reuse valid cache entries and classify everything else against S3."""
    refs = _transcript_refs(selections, contributor)
    cache = get_cache(cache_path)
    cached: list[TranscriptStatus] = []
    unresolved: list[TranscriptRef] = []
    for ref in refs:
        status = reusable_status(cache, contributor, ref)
        if status is None:
            unresolved.append(ref)
        else:
            cached.append(status)

    if on_progress:
        on_progress("checking", 0, len(unresolved))

    def progress(stage: str) -> Callable[[int, int], None]:
        return lambda done, total: on_progress(stage, done, total) if on_progress else None

    if unresolved:
        client = cast(S3HeadClient, s3 or make_s3_client())
        checked = classify_uploads(
            client,
            unresolved,
            on_check=progress("checking"),
            on_hash=progress("hashing"),
        )
    else:
        checked = []

    for status in checked:
        store_status(cache, contributor, status)
    save_cache(cache, cache_path)

    statuses = {status.transcript.identity: status for status in cached + checked}
    ordered = [statuses[ref.identity] for ref in refs if ref.identity in statuses]
    errors = [_status_error(status) for status in ordered if status.error]
    usable = any(
        status.state in {"not_uploaded", "changed", "current"}
        for status in ordered
    )
    return {
        "status": "partial" if errors and usable else "failed" if errors else "ready",
        "items": [status.as_item() for status in ordered],
        "errors": errors,
        "total": len(refs),
        "changed": sum(status.snapshot is not None for status in checked),
        "checked": len(unresolved),
        "cached": len(cached),
    }


def artifacts_for(
    selections: Selections,
    contributor: str,
    cache_path: Path | None = None,
) -> tuple[list[CacheRecord], list[PipelineItem]]:
    """Return cached upload candidates and transcripts requiring Refresh."""
    cache = get_cache(cache_path)
    candidates: list[CacheRecord] = []
    stale: list[PipelineItem] = []
    for ref in _transcript_refs(selections, contributor):
        record = get_cache_for_transcript(
            cache, contributor, ref.source.id, ref.session
        )
        if record is not None and record.get("state") == "not_uploaded":
            candidates.append(cast(CacheRecord, dict(record)))
            continue
        status = reusable_status(cache, contributor, ref)
        if status is not None and status.state == "changed" and record is not None:
            candidates.append(cast(CacheRecord, dict(record)))
        elif status is None:
            stale.append(TranscriptStatus(ref, "stale").as_item())
    return candidates, stale


def prepare_upload_artifacts(
    selections: Selections,
    candidates: list[CacheRecord],
    contributor: str,
    directory: str | Path,
    on_progress: PreparationCallback | None = None,
) -> tuple[list[ArchiveArtifact], list[dict[str, str]]]:
    """Prepare selected cached candidates concurrently."""
    refs = {
        ref.identity: ref for ref in _transcript_refs(selections, contributor)
    }
    work: list[tuple[TranscriptRef, CacheRecord]] = []
    errors: list[dict[str, str]] = []
    for candidate in candidates:
        identity = _record_identity(candidate)
        ref = refs.get(identity)
        if ref is None:
            errors.append({
                "source": str(identity[0] or ""),
                "session": str(identity[3] or ""),
                "error": "Upload candidate is no longer available; refresh and try again",
            })
        else:
            work.append((ref, candidate))

    total = len(candidates)
    completed = len(errors)
    if on_progress and completed:
        on_progress(completed, total)

    artifacts: list[ArchiveArtifact] = []
    workers = min(preparation_concurrency(), len(work))
    if workers:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_prepare_candidate, ref, candidate, contributor, directory): ref
                for ref, candidate in work
            }
            for future in as_completed(futures):
                ref = futures[future]
                completed += 1
                try:
                    artifacts.append(future.result())
                except Exception as exc:
                    errors.append({
                        "source": ref.source.id,
                        "session": ref.session.id,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                if on_progress:
                    on_progress(completed, total)
    return artifacts, errors


def mark_uploaded(
    artifacts: list[ArchiveArtifact],
    contributor: str,
    cache_path: Path | None = None,
) -> None:
    cache = get_cache(cache_path)
    uploaded = {_record_identity(item): item for item in artifacts}
    for record in cache["records"].values():
        artifact = uploaded.get(_record_identity(record))
        if record.get("contributor") != contributor or artifact is None:
            continue
        record.update(
            state="current",
            source_hash=artifact["source_hash"],
            source_hash_version=SOURCE_HASH_VERSION,
            redaction_version=REDACTION_VERSION,
            format_version=TRANSCRIPT_FORMAT_VERSION,
            filesystem_snapshot=artifact["filesystem_snapshot"],
            key=artifact["key"],
        )
        record.pop("error", None)
    save_cache(cache, cache_path)


def _transcript_refs(
    selections: Selections, contributor: str
) -> list[TranscriptRef]:
    return [
        TranscriptRef(source, session, transcript_key(contributor, source.id, session))
        for source, sessions in selections
        for session in sessions
    ]


def _prepare_candidate(
    ref: TranscriptRef,
    candidate: CacheRecord,
    contributor: str,
    directory: str | Path,
) -> ArchiveArtifact:
    previously_hashed = candidate.get("state") == "changed"
    return prepare_archive(
        ref.source,
        ref.session,
        ref.key,
        contributor,
        directory,
        expected_hash=candidate.get("source_hash") if previously_hashed else None,
        expected_filesystem_snapshot=(
            candidate.get("filesystem_snapshot") if previously_hashed else None
        ),
    )


def _record_identity(record: CacheRecord | ArchiveArtifact) -> TranscriptIdentity:
    return (
        str(record.get("source") or ""),
        str(record.get("project") or ""),
        str(record.get("parent") or ""),
        str(record.get("session") or ""),
    )


def _status_error(status: TranscriptStatus) -> dict[str, str]:
    return {
        "source": status.transcript.source.id,
        "project": status.transcript.session.project_id,
        "parent": status.transcript.session.parent or "",
        "session": status.transcript.session.id,
        "error": status.error or "Upload status unavailable",
    }
