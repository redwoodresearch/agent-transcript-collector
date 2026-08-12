"""Select, prepare, and record transcript uploads."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TypeAlias, cast

from .cache import (
    CacheRecord,
    get_cache,
    get_cache_for_transcript,
    reusable_status,
    save_cache,
)
from .prepare_archive import (
    TRANSCRIPT_FORMAT_VERSION,
    ArchiveArtifact,
    prepare_archive,
)
from .redactor import REDACTION_VERSION
from .transcript import (
    TranscriptIdentity,
    TranscriptRef,
    TranscriptSelections,
    TranscriptStatus,
    transcript_refs,
)
from .transcript_snapshot import SOURCE_HASH_VERSION

UploadItem: TypeAlias = dict[str, str | None]
PreparationCallback: TypeAlias = Callable[[int, int], None]


def archive_concurrency() -> int:
    return max(1, int(os.environ.get("CTC_ARCHIVE_CONCURRENCY", "8")))


def upload_candidates(
    selections: TranscriptSelections,
    contributor: str,
    cache_path: Path | None = None,
) -> tuple[list[CacheRecord], list[UploadItem]]:
    """Return cached upload candidates and transcripts requiring Refresh."""
    cache = get_cache(cache_path)
    candidates: list[CacheRecord] = []
    stale: list[UploadItem] = []
    for ref in transcript_refs(selections, contributor):
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


def prepare_uploads(
    selections: TranscriptSelections,
    candidates: list[CacheRecord],
    contributor: str,
    directory: str | Path,
    on_progress: PreparationCallback | None = None,
) -> tuple[list[ArchiveArtifact], list[dict[str, str]]]:
    """Prepare selected cached candidates concurrently."""
    refs = {
        ref.identity: ref for ref in transcript_refs(selections, contributor)
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
    workers = min(archive_concurrency(), len(work))
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


def record_uploaded(
    artifacts: list[ArchiveArtifact],
    contributor: str,
    cache_path: Path | None = None,
) -> None:
    """Record successfully uploaded archives as current in the local cache."""
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
