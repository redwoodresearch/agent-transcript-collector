"""Download collected transcripts from S3 to a local folder.

The inverse of the collector: where ``app.py`` uploads consented transcripts to
the shared bucket, this fetches them back for analysis. It lists the archive
units in the bucket (see :mod:`catalog`), lets you pick which to pull — by source
/ contributor / prefix on the CLI, or interactively with ``--tui`` — and writes
them under a destination folder (``./transcripts`` by default).

By default each unit zip is **extracted** into a clean per-source tree of raw
``.jsonl`` transcripts:

    <dest>/<source>/<contributor>/<group>/<session>.jsonl
    <dest>/<source>/<contributor>/_manifests/<unit>.json

Pass ``--no-extract`` to keep the raw ``.zip`` archives instead (mirrored at their
S3 key paths under ``<dest>``). Both modes are idempotent: re-running skips units
already present, so an interrupted download resumes cleanly.

Reading the bucket needs ``s3:GetObject`` + ``s3:ListBucket`` (the distributed
*upload* key is ``s3:PutObject`` only and cannot download).
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .catalog import (
    Unit,
    aggregate_by_contributor,
    aggregate_by_source,
    filter_units,
    list_units,
)
from .s3client import S3_BUCKET, make_s3_client
from .sources.base import human_size

DEFAULT_DEST = Path("transcripts")
DOWNLOAD_CONCURRENCY = max(1, int(os.environ.get("CTC_DOWNLOAD_CONCURRENCY", "4")))
MANIFEST_DIRNAME = "_manifests"


def _unit_base(unit: Unit, dest: Path) -> Path:
    """Directory a unit's extracted contents live under (joining "" is a no-op)."""
    return dest / unit.source / unit.contributor


def _manifest_marker(unit: Unit, dest: Path) -> Path:
    """Per-unit completion marker (also holds the unit's manifest.json when present)."""
    stem = Path(unit.key).name[: -len(".zip")]
    return _unit_base(unit, dest) / MANIFEST_DIRNAME / f"{stem}.json"


def _is_done(unit: Unit, dest: Path, extract: bool) -> bool:
    if extract:
        return _manifest_marker(unit, dest).exists()
    target = dest / unit.key
    return target.exists() and target.stat().st_size == unit.size


def _extract_unit(unit: Unit, body: bytes, dest: Path) -> None:
    base = _unit_base(unit, dest)
    manifest_bytes = b""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if Path(name).name == "manifest.json":
                manifest_bytes = zf.read(name)
                continue
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
    # Write the completion marker last so a crash mid-extract is retried, not skipped.
    marker = _manifest_marker(unit, dest)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(manifest_bytes)


def _save_zip(unit: Unit, body: bytes, dest: Path) -> None:
    target = dest / unit.key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def _fetch_one(s3, bucket: str, unit: Unit, dest: Path, extract: bool) -> str:
    """Download+store one unit. Returns a status: 'ok', 'skip', or 'error: ...'."""
    if _is_done(unit, dest, extract):
        return "skip"
    try:
        body = s3.get_object(Bucket=bucket, Key=unit.key)["Body"].read()
        if extract:
            _extract_unit(unit, body, dest)
        else:
            _save_zip(unit, body, dest)
        return "ok"
    except Exception as exc:  # noqa: BLE001 - report and continue, never abort the batch
        return f"error: {exc}"


def download_units(
    s3,
    bucket: str,
    units: list[Unit],
    dest: Path,
    extract: bool = True,
    concurrency: int = DOWNLOAD_CONCURRENCY,
) -> tuple[int, int, list[tuple[Unit, str]]]:
    """Download ``units`` into ``dest``. Returns (ok, skipped, errors)."""
    dest.mkdir(parents=True, exist_ok=True)
    total = len(units)
    counter = {"done": 0}
    lock = threading.Lock()
    ok = 0
    skipped = 0
    errors: list[tuple[Unit, str]] = []

    def work(unit: Unit) -> tuple[Unit, str]:
        status = _fetch_one(s3, bucket, unit, dest, extract)
        with lock:
            counter["done"] += 1
            n = counter["done"]
        verb = {"ok": "✓", "skip": "·"}.get(status, "✗")
        print(f"[{n}/{total}] {verb} {unit.key} ({unit.size_human})")
        return unit, status

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for unit, status in pool.map(work, units):
            if status == "ok":
                ok += 1
            elif status == "skip":
                skipped += 1
            else:
                errors.append((unit, status))
    return ok, skipped, errors


def print_catalog(units: list[Unit], verbose: bool) -> None:
    """Print available units grouped by source (and contributor when verbose)."""
    if not units:
        print("No transcript archives found.")
        return
    by_source = aggregate_by_source(units)
    total_count = sum(a.count for a in by_source.values())
    total_bytes = sum(a.bytes for a in by_source.values())
    print(f"{'SOURCE':<22} {'ZIPS':>8} {'SIZE':>12}")
    print("-" * 44)
    for source, agg in by_source.items():
        print(f"{source:<22} {agg.count:>8} {agg.size_human:>12}")
        if verbose:
            for (src, contributor), c_agg in aggregate_by_contributor(units).items():
                if src != source:
                    continue
                label = contributor or "(no contributor)"
                print(f"    {label:<18} {c_agg.count:>8} {c_agg.size_human:>12}")
    print("-" * 44)
    print(f"{'TOTAL':<22} {total_count:>8} {human_size(total_bytes):>12}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-transcript-downloader",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"Destination folder (default: ./{DEFAULT_DEST}).",
    )
    parser.add_argument(
        "--source",
        action="append",
        metavar="SOURCE",
        help="Only this source, e.g. claude_code (repeatable).",
    )
    parser.add_argument(
        "--contributor",
        action="append",
        metavar="NAME",
        help="Only this contributor/collection segment (repeatable).",
    )
    parser.add_argument(
        "--prefix",
        metavar="S3PREFIX",
        help="Only keys starting with this S3 prefix, e.g. claude_code/alice/.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List what's available (filtered, if filters given) and exit.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="With --list, also break down by contributor.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download everything matched without interactive selection.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Open an interactive selector to pick sources (needs the 'tui' extra).",
    )
    parser.add_argument(
        "--extract",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Extract zips into a .jsonl tree (default). --no-extract keeps raw zips.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DOWNLOAD_CONCURRENCY,
        help=f"Parallel downloads (default: {DOWNLOAD_CONCURRENCY}, $CTC_DOWNLOAD_CONCURRENCY).",
    )
    return parser


def _s3_prefix_hint(args: argparse.Namespace) -> str | None:
    """A narrowing Prefix for the S3 listing, when the filters allow one."""
    if args.prefix:
        return args.prefix
    if args.source and len(args.source) == 1 and not args.contributor:
        return f"{args.source[0]}/"
    return None


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    s3 = make_s3_client()
    print(f"Listing s3://{S3_BUCKET} ...", file=sys.stderr)
    units = list_units(s3, S3_BUCKET, prefix=_s3_prefix_hint(args))
    units = filter_units(units, args.source, args.contributor, args.prefix)

    if args.list:
        print_catalog(units, args.verbose)
        return 0

    if args.tui:
        try:
            from .tui import select_sources
        except ImportError:
            print(
                "The --tui selector needs the 'tui' extra. Install it with:\n"
                "  uv pip install 'agent-transcript-collector[tui]'\n"
                "or filter non-interactively with --source / --prefix / --all.",
                file=sys.stderr,
            )
            return 2
        chosen = select_sources(aggregate_by_source(units))
        if not chosen:
            print("Nothing selected; aborting.", file=sys.stderr)
            return 1
        units = filter_units(units, sources=chosen)
    elif not (args.all or args.source or args.contributor or args.prefix):
        print_catalog(units, args.verbose)
        print(
            "\nNothing downloaded. Choose what to pull with --tui, --all, "
            "--source, --contributor, or --prefix.",
            file=sys.stderr,
        )
        return 0

    if not units:
        print("Nothing matched your filters.", file=sys.stderr)
        return 1

    total_bytes = sum(u.size for u in units)
    print(
        f"Downloading {len(units)} archive(s) (~{human_size(total_bytes)}) "
        f"to {args.dest} ...",
        file=sys.stderr,
    )
    ok, skipped, errors = download_units(
        s3, S3_BUCKET, units, args.dest, args.extract, args.concurrency
    )
    print(f"\nDone: {ok} downloaded, {skipped} already present, {len(errors)} failed.")
    for unit, status in errors:
        print(f"  {unit.key}: {status}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
