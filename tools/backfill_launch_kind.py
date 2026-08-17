"""Backfill launch kind onto already-uploaded archives for one contributor.

Reads each archive, classifies the transcript inside it, and rewrites the
archive with `launch_kind` in manifest.json, in the packaged ATIF trajectory
(including per-step prompt origins), and in the object's S3 metadata. The
transcript bytes and every other archive member are copied through untouched,
so the collector's source-hash bookkeeping still matches and nothing is
re-redacted.

Usage:
    uv run python tools/backfill_launch_kind.py <contributor> [--apply]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile

from agent_transcript_collector.atif import ATIF_FILENAME
from agent_transcript_collector.launch_kind import launch_kind
from agent_transcript_collector.s3client import make_s3_client
from agent_transcript_collector.storage import transcript_filename
from agent_transcript_collector.uploader import LAUNCH_KIND_METADATA, S3_BUCKET

TRANSCRIPT_NAMES = ("transcript.jsonl", "transcript.txt")


def _transcript_bytes(archive: zipfile.ZipFile) -> str | None:
    for name in TRANSCRIPT_NAMES:
        if name in archive.namelist():
            return archive.read(name).decode("utf-8", errors="replace")
    return None


def _annotate_atif(trajectory: dict, raw: str, kind: str) -> dict:
    """Mirror the collector's ATIF annotation on an already-built trajectory."""
    from agent_transcript_collector.atif import _prompt_origin_index

    collector = dict(trajectory.get("extra", {}).get("agent_transcript_collector", {}))
    collector["launch_kind"] = kind
    trajectory["extra"] = {**trajectory.get("extra", {}), "agent_transcript_collector": collector}

    index = _prompt_origin_index(raw)
    for step in trajectory.get("steps", []):
        if step.get("source") != "user":
            continue
        message = step.get("message")
        if isinstance(message, list):
            message = " ".join(
                str(part.get("text", "")) for part in message if isinstance(part, dict)
            )
        annotation = index.get(" ".join(str(message or "").split()))
        if annotation:
            step["extra"] = {**(step.get("extra") or {}), **annotation}
    return trajectory


def rebuild(payload: bytes, inherited: str | None = None) -> tuple[bytes, str]:
    source = zipfile.ZipFile(io.BytesIO(payload))
    raw = _transcript_bytes(source)
    if raw is None:
        raise ValueError("archive has no transcript member")
    kind = launch_kind(raw)
    # A subagent has no prompts of its own; it belongs to its parent's run.
    if kind == "unknown" and inherited:
        kind = inherited

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["launch_kind"] = kind
                data = json.dumps(manifest, indent=2).encode()
            elif item.filename == ATIF_FILENAME:
                trajectory = _annotate_atif(json.loads(data), raw, kind)
                data = (json.dumps(trajectory, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            target.writestr(item.filename, data)
    return buffer.getvalue(), kind


def _parent_kind(s3, payload: bytes, key: str) -> str | None:
    """Classify the parent run of a subagent archive, if there is one."""
    try:
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(payload)).read("manifest.json"))
    except (KeyError, ValueError):
        return None
    parent_id = manifest.get("parent_id")
    if not parent_id:
        return None
    source = str(manifest.get("source", {}).get("type") or "")
    parent_session = parent_id.split("--", 1)[-1]
    parent_key = f"{key.rsplit('/', 1)[0]}/{transcript_filename(source, parent_session)}"
    try:
        parent_payload = s3.get_object(Bucket=S3_BUCKET, Key=parent_key)["Body"].read()
        parent_raw = _transcript_bytes(zipfile.ZipFile(io.BytesIO(parent_payload)))
    except Exception:
        return None
    return launch_kind(parent_raw) if parent_raw else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("contributor")
    parser.add_argument("--apply", action="store_true", help="write changes back to S3")
    args = parser.parse_args()

    s3 = make_s3_client()
    prefix = f"mts-trans/{args.contributor}/"
    keys = []
    token = None
    while True:
        page = s3.list_objects_v2(**{
            "Bucket": S3_BUCKET, "Prefix": prefix,
            **({"ContinuationToken": token} if token else {}),
        })
        keys.extend(item["Key"] for item in page.get("Contents", []) if item["Key"].endswith(".zip"))
        token = page.get("NextContinuationToken")
        if not token:
            break

    print(f"{len(keys)} archive(s) under {prefix}")
    changed = skipped = failed = 0
    for key in keys:
        try:
            head = s3.head_object(Bucket=S3_BUCKET, Key=key)
            existing = head.get("Metadata", {})
            # "unknown" is worth retrying: a subagent can inherit its parent now.
            if existing.get(LAUNCH_KIND_METADATA) in {"human", "programmatic"}:
                print(f"  skip    {key} (already {existing[LAUNCH_KIND_METADATA]})")
                skipped += 1
                continue
            fetched = s3.get_object(Bucket=S3_BUCKET, Key=key)
            payload = fetched["Body"].read()
            etag = fetched.get("ETag")
            rebuilt, kind = rebuild(payload, inherited=_parent_kind(s3, payload, key))
            if rebuilt == payload and existing.get(LAUNCH_KIND_METADATA) == kind:
                print(f"  skip    {key} (already {kind}, archive unchanged)")
                skipped += 1
                continue
            print(f"  {'write ' if args.apply else 'would '} {key} -> {kind}")
            if args.apply:
                # The watcher uploads to this same prefix on a timer. Without a
                # precondition a re-collected archive could be reverted, or its
                # body paired with this run's stale metadata.
                current = s3.head_object(Bucket=S3_BUCKET, Key=key)
                if current.get("ETag") != etag:
                    print(f"  SKIP    {key}: changed while this ran; rerun to pick it up")
                    skipped += 1
                    continue
                s3.put_object(
                    Bucket=S3_BUCKET, Key=key, Body=rebuilt,
                    ContentType="application/zip",
                    Metadata={**existing, LAUNCH_KIND_METADATA: kind},
                    **({"StorageClass": current["StorageClass"]} if current.get("StorageClass") else {}),
                )
            changed += 1
        except Exception as exc:  # keep going; one bad archive should not stop the run
            print(f"  FAILED  {key}: {type(exc).__name__}: {exc}")
            failed += 1

    verb = "updated" if args.apply else "would update"
    print(f"\n{verb} {changed}, skipped {skipped}, failed {failed}")
    if not args.apply:
        print("dry run only; pass --apply to write")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
