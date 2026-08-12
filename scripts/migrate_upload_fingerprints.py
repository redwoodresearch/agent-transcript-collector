"""One-off migration from legacy upload hashes to original-content hashes.

This script is intentionally not installed with the collector. Delete it after
the production migration has been run and verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from botocore.exceptions import ClientError

from agent_transcript_collector.migrate import migrate_config
from agent_transcript_collector.paths import pipeline_cache_path, watcher_config_path
from agent_transcript_collector.redactor import canonicalize_secrets, redact_identity
from agent_transcript_collector.s3client import S3_BUCKET, make_s3_client
from agent_transcript_collector.uploader import (
    BODY_FINGERPRINT_METADATA,
    FINGERPRINT_METADATA,
    FINGERPRINT_VERSION,
    FINGERPRINT_VERSION_METADATA,
    FORMAT_VERSION_METADATA,
    REDACTION_VERSION,
    REDACTION_VERSION_METADATA,
    SIDECAR_COUNT_METADATA,
    TRANSCRIPT_FORMAT_VERSION,
    UploadLock,
    metadata_concurrency,
    prepare_transcript,
)
from agent_transcript_collector.watcher import discover_allowed

LEGACY_FINGERPRINT_VERSION = 2


def _legacy_privacy_safe_digest(text: str) -> str:
    canonical, _ = redact_identity(canonicalize_secrets(text))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _legacy_body_fingerprint(raw_bytes: bytes) -> str:
    policy = f"archive-v{LEGACY_FINGERPRINT_VERSION}:redact-id=1\0"
    canonical = canonicalize_secrets(raw_bytes.decode("utf-8", errors="replace"))
    canonical, _ = redact_identity(canonical)
    return hashlib.sha256(policy.encode() + canonical.encode()).hexdigest()


def _legacy_content_fingerprint(body_fingerprint: str, sidecars) -> str:
    if not sidecars.files:
        return body_fingerprint
    digest = hashlib.sha256(f"sidecars-v1\0{body_fingerprint}".encode())
    for sidecar in sidecars.files:
        try:
            text = sidecar.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        digest.update(
            f"{sidecar.arcname}\0{_legacy_privacy_safe_digest(text)}\0".encode()
        )
    return digest.hexdigest()


def _head(s3, key: str) -> dict | None:
    try:
        return s3.head_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _remote_is_current(prepared, metadata: dict) -> bool:
    versions = {
        FINGERPRINT_VERSION_METADATA: str(FINGERPRINT_VERSION),
        REDACTION_VERSION_METADATA: str(REDACTION_VERSION),
        FORMAT_VERSION_METADATA: str(TRANSCRIPT_FORMAT_VERSION),
    }
    if any(metadata.get(key) != value for key, value in versions.items()):
        return False
    if metadata.get(FINGERPRINT_METADATA) == prepared.fingerprint:
        return True
    try:
        remote_sidecars = int(metadata.get(SIDECAR_COUNT_METADATA, "0"))
    except ValueError:
        return False
    return (
        metadata.get(BODY_FINGERPRINT_METADATA) == prepared.body_fingerprint
        and len(prepared.sidecars.files) < remote_sidecars
    )


def _replacement_metadata(prepared, metadata: dict) -> dict | None:
    """Return migrated metadata only when legacy hashes prove the body matches."""
    version_keys = {
        FINGERPRINT_VERSION_METADATA,
        REDACTION_VERSION_METADATA,
        FORMAT_VERSION_METADATA,
    }
    if version_keys & metadata.keys():
        return None

    legacy_body = _legacy_body_fingerprint(prepared.raw_bytes)
    if metadata.get(BODY_FINGERPRINT_METADATA) != legacy_body:
        return None
    try:
        remote_sidecars = int(metadata.get(SIDECAR_COUNT_METADATA, "0"))
    except ValueError:
        return None

    legacy_content = _legacy_content_fingerprint(legacy_body, prepared.sidecars)
    if metadata.get(FINGERPRINT_METADATA) == legacy_content:
        source_fingerprint = prepared.fingerprint
    elif (
        metadata.get(FINGERPRINT_METADATA)
        and len(prepared.sidecars.files) < remote_sidecars
    ):
        source_fingerprint = f"legacy-fuller:{metadata[FINGERPRINT_METADATA]}"
    else:
        return None

    return metadata | {
        FINGERPRINT_METADATA: source_fingerprint,
        BODY_FINGERPRINT_METADATA: prepared.body_fingerprint,
        FINGERPRINT_VERSION_METADATA: str(FINGERPRINT_VERSION),
        REDACTION_VERSION_METADATA: str(REDACTION_VERSION),
        FORMAT_VERSION_METADATA: str(TRANSCRIPT_FORMAT_VERSION),
    }


def _copy_with_metadata(s3, key: str, head: dict, metadata: dict) -> None:
    request = {
        "Bucket": S3_BUCKET,
        "Key": key,
        "CopySource": {"Bucket": S3_BUCKET, "Key": key},
        "CopySourceIfMatch": head["ETag"],
        "Metadata": metadata,
        "MetadataDirective": "REPLACE",
    }
    for field in (
        "CacheControl",
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "WebsiteRedirectLocation",
    ):
        if head.get(field) is not None:
            request[field] = head[field]
    s3.copy_object(**request)


def migrate_uploads(
    selections,
    contributor: str,
    s3,
    *,
    dry_run: bool = False,
    on_progress=None,
) -> dict:
    work = [
        (source, session)
        for source, sessions in selections
        for session in sessions
    ]
    result = {
        "status": "completed",
        "dry_run": dry_run,
        "total": len(work),
        "eligible": 0,
        "migrated": 0,
        "current": 0,
        "missing": 0,
        "needs_upload": 0,
        "errors": [],
    }

    def migrate_one(source, session):
        prepared = prepare_transcript(source, session, contributor)
        head = _head(s3, prepared.key)
        if head is None:
            return "missing"
        metadata = head.get("Metadata", {})
        if _remote_is_current(prepared, metadata):
            return "current"
        replacement = _replacement_metadata(prepared, metadata)
        if replacement is None:
            return "needs_upload"
        if dry_run:
            return "eligible"
        _copy_with_metadata(s3, prepared.key, head, replacement)
        return "migrated"

    workers = max(1, min(metadata_concurrency(), len(work)))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(migrate_one, source, session): (source, session)
            for source, session in work
        }
        for future in as_completed(futures):
            source, session = futures[future]
            completed += 1
            try:
                outcome = future.result()
            except Exception as exc:
                result["errors"].append({
                    "source": source.id,
                    "session": session.id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                result[outcome] += 1
            if on_progress:
                on_progress(completed, len(work))
    if result["errors"]:
        result["status"] = "partial"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=watcher_config_path())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = migrate_config(args.config)
    os.environ.update(config.source_env)
    os.environ["CTC_AWS_PROFILE"] = config.aws_profile
    selections = discover_allowed(config)

    def progress(done, total):
        print(f"\rChecked {done}/{total} uploads", end="", file=sys.stderr)

    with UploadLock():
        result = migrate_uploads(
            selections,
            config.contributor,
            make_s3_client(),
            dry_run=args.dry_run,
            on_progress=progress,
        )
        if not args.dry_run:
            pipeline_cache_path().unlink(missing_ok=True)
    if result["total"]:
        print(file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
