"""Import raw *chippy* run transcripts into the shared research bucket.

Where ``app.py`` collects transcripts from a contributor's own machine with
consent, this is the **admin-side importer** for an existing archive that already
lives in S3: the chippy controller writes raw, unredacted run transcripts to its
own artifacts bucket, and this tool mirrors them locally, redacts them, and
re-uploads one zip per run to ``rr-agent-transcripts`` for analysis.

    source  s3://<source-bucket>/<source-prefix>/<run-id>/transcripts/*.jsonl   (raw)
              │  read with a source-account profile (s3:GetObject + s3:ListBucket)
              ▼
    mirror  <mirror>/<run-id>/transcripts/*.jsonl                               (raw cache)
              │  redact locally (secrets + emails + home paths + names + handles)
              ▼
    dest    s3://<dest-bucket>/<dest-prefix>/<run-id>/transcripts.zip           (redacted)
              │  written with a dest-account profile (s3:PutObject)

Because source and dest usually live in **different AWS accounts**, source and
dest clients take independent ``--source-profile`` / ``--dest-profile`` (or the
default credential chain). Both stages are idempotent: mirrored files are not
re-downloaded, and runs already present in the dest bucket are skipped unless
``--force``.

Redaction reuses the package :mod:`redactor` for secrets and composes its
identity helpers with an importer-specific policy: a curated automated-email
keep-list, plus personal names / GitHub handles that are either passed in
(``--redact-names``, ``--redact-handles-file``) or discovered automatically with
``--llm-screen`` (see :mod:`chippy_screen`). No personal identifiers are baked
into this repo.
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3

from . import redactor

SOURCE_BUCKET = os.environ.get(
    "CHIPPY_SOURCE_BUCKET", "chippy-controller-artifacts-136268833242-us-east-1"
)
SOURCE_PREFIX = os.environ.get("CHIPPY_SOURCE_PREFIX", "runs/")
DEST_BUCKET = os.environ.get("CTC_S3_BUCKET", "rr-agent-transcripts")
DEST_PREFIX = os.environ.get("CHIPPY_DEST_PREFIX", "chippy")
DEFAULT_MIRROR = Path(os.environ.get("CHIPPY_MIRROR", "chippy-mirror"))
SOURCE_FORMAT = "chippy-claude-derived-jsonl"

# Automated / bot senders that carry no personal information and are useful to
# keep for research (so a reader can see which messages were machine-generated).
# Safe to ship: none of these identify a person.
AUTOMATED_EMAILS = frozenset({
    "noreply@anthropic.com",
    "claude@anthropic.com",
    "nclaude@anthropic.com",
    "research-agent@anthropic.com",
    "anthropic_assistant@anthropic.com",
    "ai@anthropic.com",
    "cursoragent@cursor.com",
    "git@github.com",
    "noreply@github.com",
    "packages@pytorch.org",
})

_REDKEYS = ("secret", "email", "home_path", "username", "handle")


class RedactionPolicy:
    """The importer-specific redaction knobs, resolved once per run."""

    def __init__(self, keep_emails=AUTOMATED_EMAILS, names=(), handles=()):
        self.keep_emails = frozenset(keep_emails)
        self.names = set(names)
        self.handles = set(handles)

    def extend(self, names=(), handles=()):
        """Merge in additional personal names / handles (e.g. from LLM screening)."""
        self.names.update(names)
        self.handles.update(handles)

    def apply(self, text: str) -> tuple[str, dict]:
        """Redact one transcript's text. Returns (redacted_text, per-kind counts).

        Order matters: secrets first (so a secret that looks like an email/path
        is mocked as a secret), then emails before bare names (so ``ryan@x.com``
        becomes ``[EMAIL]`` rather than ``[USER]@x.com``).
        """
        counts: dict[str, int] = {}
        text, counts["secret"] = redactor.redact_jsonl_content(text)
        text, counts["email"] = redactor.redact_emails(text, keep=self.keep_emails)
        text, counts["home_path"] = redactor.redact_home_path_users(text)
        text, counts["username"] = redactor.redact_named_users(text, self.names)
        text, counts["handle"] = redactor.redact_github_handles(text, self.handles)
        counts["total"] = sum(counts.values())
        return text, counts


# --------------------------------------------------------------------------- #
# S3 helpers
# --------------------------------------------------------------------------- #

def make_client(profile: str | None):
    """S3 client for a named profile, else boto3's default credential chain."""
    region = os.environ.get("CTC_S3_REGION", "us-east-1")
    session = boto3.session.Session(profile_name=profile) if profile else boto3.session.Session()
    return session.client("s3", region_name=region)


def list_source_runs(s3, bucket: str, prefix: str) -> dict[str, list[str]]:
    """Map ``run-id -> sorted transcript keys`` for every run under ``prefix``."""
    runs: dict[str, list[str]] = collections.defaultdict(list)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if "/transcripts/" not in key or not key.endswith(".jsonl"):
                continue
            rest = key[len(prefix):] if key.startswith(prefix) else key
            run_id = rest.split("/", 1)[0]
            if run_id:
                runs[run_id].append(key)
    return {rid: sorted(keys) for rid, keys in runs.items()}


def dest_key(run_id: str) -> str:
    return f"{DEST_PREFIX}/{run_id}/transcripts.zip"


def dest_exists(s3, bucket: str, run_id: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=dest_key(run_id))
        return True
    except Exception:  # noqa: BLE001 - any error (incl. 404) means "not present"
        return False


def read_transcript(s3, bucket: str, key: str, mirror: Path) -> str:
    """Return a transcript's text, caching the raw bytes in the local mirror.

    Already-mirrored files are read from disk (never re-downloaded), so re-runs
    are cheap and survive credential expiry.
    """
    local = mirror / key
    if local.exists():
        return local.read_text(encoding="utf-8", errors="replace")
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(body)
    return body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Zip building
# --------------------------------------------------------------------------- #

def build_run_zip(
    s3, source_bucket: str, run_id: str, keys: list[str], policy: RedactionPolicy, mirror: Path
) -> tuple[bytes, dict]:
    """Redact every transcript in a run and bundle them + a manifest into a zip."""
    buf = io.BytesIO()
    entries = []
    totals: collections.Counter = collections.Counter()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in keys:
            raw = read_transcript(s3, source_bucket, key, mirror)
            red, counts = policy.apply(raw)
            arc = key.split(f"/{run_id}/", 1)[1]  # -> transcripts/...
            zf.writestr(arc, red)
            for k in _REDKEYS:
                totals[k] += counts.get(k, 0)
            entries.append({
                "path": arc,
                "is_subagent": "/subagents/" in arc,
                "size_bytes": len(red.encode("utf-8")),
                "redactions": {k: counts.get(k, 0) for k in _REDKEYS},
            })
        manifest = {
            "source": "chippy",
            "source_format": SOURCE_FORMAT,
            "run_id": run_id,
            "imported_from": f"s3://{source_bucket}/{SOURCE_PREFIX}{run_id}/transcripts/",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "redaction_scope": ["secrets", "emails(real,minus-automated)",
                                "home-path-usernames", "personal-names", "github-handles"],
            "transcript_count": len(entries),
            "subagent_count": sum(e["is_subagent"] for e in entries),
            "redaction_totals": {k: totals[k] for k in _REDKEYS},
            "transcripts": entries,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue(), manifest


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def select_runs(runs: dict[str, list[str]], only, limit: int) -> list[tuple[str, list[str]]]:
    ordered = sorted(runs.items(), key=lambda kv: len(kv[1]))  # smallest first
    if only:
        return [(r, runs[r]) for r in only if r in runs]
    if limit:
        return ordered[:limit]
    return ordered


def resolve_policy(args) -> RedactionPolicy:
    """Build the redaction policy from CLI flags (static config only).

    LLM screening, if enabled, extends this policy in-place from the actually
    selected runs — see ``import_runs`` — so it can see what's really present
    rather than a hand-maintained list.
    """
    names: set[str] = set()
    handles: set[str] = set()
    if args.redact_names:
        names.update(n for n in args.redact_names.split(",") if n.strip())
    if args.redact_names_file:
        names.update(_read_list(args.redact_names_file))
    if args.redact_handles_file:
        handles.update(_read_list(args.redact_handles_file))

    keep = set(AUTOMATED_EMAILS)
    if args.keep_emails_file:
        keep.update(_read_list(args.keep_emails_file))
    return RedactionPolicy(keep_emails=frozenset(keep), names=names, handles=handles)


def _read_list(path: str) -> list[str]:
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def require_handle_policy(args) -> None:
    """Refuse to run until the operator has made an explicit GitHub-handle decision.

    Personal GitHub handles are only scrubbed when screened in, so — unlike
    secrets and emails, which redact by default — an import with no handle policy
    would silently publish real people's handles. Rather than let that happen on a
    forgotten flag, require one of: ``--llm-screen`` (recommended — classifies
    handles automatically), ``--redact-handles-file`` (a known list), or an
    explicit ``--keep-all-handles`` acknowledgement.
    """
    if args.llm_screen or args.redact_handles_file or args.keep_all_handles:
        return
    raise SystemExit(
        "refusing to import: no GitHub-handle policy set, so personal handles "
        "would be published unredacted.\n"
        "Choose one:\n"
        "  --llm-screen              (recommended) classify handles as personal "
        "vs org/bot with an LLM and redact the personal ones\n"
        "  --redact-handles-file P   redact the handles listed in file P\n"
        "  --keep-all-handles        keep every handle as-is (only when handles "
        "carry no personal info)"
    )


def import_runs(args) -> int:
    require_handle_policy(args)

    src = make_client(args.source_profile)
    dst = None if args.dry_run else make_client(args.dest_profile)

    print(f"Listing s3://{args.source_bucket}/{args.source_prefix} ...", file=sys.stderr)
    runs = list_source_runs(src, args.source_bucket, args.source_prefix)
    selected = select_runs(runs, args.run, args.limit)
    print(f"{len(runs)} run(s) in source; {len(selected)} selected.", file=sys.stderr)

    policy = resolve_policy(args)

    args.mirror.mkdir(parents=True, exist_ok=True)
    if args.llm_screen:
        from .chippy_screen import screen_runs  # lazy: needs the 'llm' extra
        transcripts = (read_transcript(src, args.source_bucket, key, args.mirror)
                       for _rid, keys in selected for key in keys)
        screened = screen_runs(transcripts, model=args.llm_model)
        policy.extend(names=screened.names, handles=screened.handles)
        print(f"LLM screening ({args.llm_model}): +{len(screened.names)} names, "
              f"+{len(screened.handles)} handles to redact.", file=sys.stderr)

    if args.keep_all_handles and not args.llm_screen and not args.redact_handles_file:
        print("NOTE: --keep-all-handles — every github.com/<handle> is kept as-is, "
              "including real people's accounts. --llm-screen is the recommended "
              "alternative (it scrubs personal handles, keeps orgs/bots).",
              file=sys.stderr)
    if not policy.names:
        print("NOTE: no personal names configured (--redact-names / --redact-names-file); "
              "personal names in free text / paths won't be scrubbed unless --llm-screen "
              "classifies them.", file=sys.stderr)
    if args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)

    done = skipped = 0
    grand: collections.Counter = collections.Counter()
    for run_id, keys in selected:
        if not args.dry_run and not args.force and dest_exists(dst, args.dest_bucket, run_id):
            skipped += 1
            continue
        data, manifest = build_run_zip(src, args.source_bucket, run_id, keys, policy, args.mirror)
        for k in _REDKEYS:
            grand[k] += manifest["redaction_totals"][k]
        tag = (f"{manifest['transcript_count']}t/{manifest['subagent_count']}sub "
               f"red={manifest['redaction_totals']} {len(data) / 1e6:.2f}MB")
        if args.dry_run:
            (args.out / f"{run_id}.zip").write_bytes(data)
            print(f"DRY {run_id} {tag}")
        else:
            dst.put_object(Bucket=args.dest_bucket, Key=dest_key(run_id), Body=data,
                           ContentType="application/zip")
            print(f"UP  {run_id} {tag} -> s3://{args.dest_bucket}/{dest_key(run_id)}", flush=True)
        done += 1

    verb = "would import" if args.dry_run else "imported"
    print(f"\nDone: {verb} {done} run(s), {skipped} already present. "
          f"redaction totals: {dict(grand)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chippy-importer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source-bucket", default=SOURCE_BUCKET,
                   help=f"Source bucket (default: {SOURCE_BUCKET}, $CHIPPY_SOURCE_BUCKET).")
    p.add_argument("--source-prefix", default=SOURCE_PREFIX,
                   help=f"Source key prefix (default: {SOURCE_PREFIX}).")
    p.add_argument("--source-profile", default=os.environ.get("CHIPPY_SOURCE_PROFILE"),
                   help="AWS profile for the SOURCE account (default: env/default chain).")
    p.add_argument("--dest-bucket", default=DEST_BUCKET,
                   help=f"Destination bucket (default: {DEST_BUCKET}, $CTC_S3_BUCKET).")
    p.add_argument("--dest-profile", default=os.environ.get("CHIPPY_DEST_PROFILE"),
                   help="AWS profile for the DEST account (default: env/default chain).")
    p.add_argument("--mirror", type=Path, default=DEFAULT_MIRROR,
                   help=f"Local raw-cache dir (default: ./{DEFAULT_MIRROR}, $CHIPPY_MIRROR).")
    p.add_argument("--run", action="append", metavar="RUN_ID",
                   help="Only this run id (repeatable).")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Import only the N smallest runs (for a trial).")
    p.add_argument("--force", action="store_true",
                   help="Re-import runs even if already present in the dest bucket.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build zips into --out without uploading.")
    p.add_argument("--out", type=Path, default=Path("chippy-out"),
                   help="Output dir for --dry-run zips (default: ./chippy-out).")
    # Redaction policy
    p.add_argument("--redact-names", metavar="A,B,C",
                   help="Comma-separated personal names to scrub as bare tokens.")
    p.add_argument("--redact-names-file", metavar="PATH",
                   help="File of personal names (one per line) to scrub.")
    p.add_argument("--redact-handles-file", metavar="PATH",
                   help="File of personal GitHub handles (one per line) to scrub.")
    p.add_argument("--keep-all-handles", action="store_true",
                   help="Acknowledge that every github.com/<handle> is kept as-is "
                        "(incl. real people). Only for handle-free corpora; prefer --llm-screen.")
    p.add_argument("--keep-emails-file", metavar="PATH",
                   help="File of additional automated emails to KEEP (one per line).")
    p.add_argument("--llm-screen", action="store_true",
                   help="RECOMMENDED. Use an LLM to classify candidate names/handles as personal "
                        "(redact) vs org/bot (keep). Needs the 'llm' extra and ANTHROPIC_API_KEY "
                        "(or an 'ant auth login' profile).")
    p.add_argument("--llm-model", default=os.environ.get("CHIPPY_LLM_MODEL", "claude-opus-4-8"),
                   help="Model for --llm-screen (default: claude-opus-4-8).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return import_runs(args)


if __name__ == "__main__":
    raise SystemExit(main())
