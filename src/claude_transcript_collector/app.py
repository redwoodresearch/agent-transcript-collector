"""FastAPI app: local web UI for selecting and uploading agent transcripts.

Supports multiple agent harnesses (Claude Code, Codex, Pi) via the source
adapters in `.sources`. Uploads run as a background job and are split into
size-budgeted, resumable units keyed
<bucket>/<source>/<contributor>/<group-hash>/part-NNN-<members-hash>.zip
"""

import hashlib
import io
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, PackageLoader

from .redactor import redact_identity, redact_jsonl_content, redact_path_token
from .sources import SOURCES, detect_all, find_session, get_source

S3_BUCKET = os.environ.get("CTC_S3_BUCKET", "rr-agent-transcripts")
S3_REGION = os.environ.get("CTC_S3_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.environ.get("CTC_AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("CTC_AWS_SECRET_ACCESS_KEY", "")


def _make_s3_client():
    """Build an S3 client.

    Use the explicit CTC_AWS_* credentials when both are provided; otherwise
    fall back to boto3's default credential chain (standard AWS env vars,
    shared config/credentials files, SSO, instance/container roles). Passing
    empty strings explicitly would override that chain, so we omit them.
    """
    kwargs = {"region_name": S3_REGION}
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def _safe_name(name: str) -> str:
    """Sanitize a contributor name for use as an S3 key segment."""
    name = (name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return name or "anonymous"


app = FastAPI()

jinja_env = Environment(
    loader=PackageLoader("claude_transcript_collector", "templates"),
    autoescape=True,
)


@app.get("/", response_class=HTMLResponse)
async def index():
    sources = detect_all()
    template = jinja_env.get_template("index.html")
    return template.render(sources=sources)


@app.get("/api/preview")
async def preview_session(source: str, group: str, session: str, parent: str = "",
                          identity: bool = True):
    """Preview a session's messages. Secrets are always redacted; identity is
    the only optional pass."""
    sess = find_session(source, group, session, parent or None)
    src = get_source(source)
    if sess is None or src is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")

    raw, redaction_count = redact_jsonl_content(raw)   # always — secrets/credentials
    if identity:
        raw, n = redact_identity(raw)
        redaction_count += n

    messages = []
    for m in src.parse_messages(raw):
        text = m["text"]
        messages.append({
            "role": m["role"],
            "text": text[:2000] + ("..." if len(text) > 2000 else ""),
        })

    return {
        "messages": messages,
        "redaction_count": redaction_count,
        "total_messages": len(messages),
    }


# Per-unit byte budget: caps each upload object so an abort loses at most one
# small object. Keys are deterministic, so completed units stay durable and a
# re-run overwrites them in place (idempotent — no duplicates).
UNIT_BYTES = int(os.environ.get("CTC_UNIT_BYTES", str(25 * 1024 * 1024)))


def _group_token(group_key):
    # Opaque, deterministic, key-safe, leak-proof regardless of the identity
    # toggle (the redacted label still travels inside the manifest).
    return "g" + hashlib.sha1(group_key.encode("utf-8")).hexdigest()[:12]


def _members_hash(unit_sessions):
    # Membership-addressed: the same selection re-uploads to the same key
    # (overwrite-in-place). A *different* selection yields a different key, so the
    # previous parts remain as orphan objects (harmless; dedup downstream by id).
    ids = "\n".join(f"{s.parent or ''}/{s.id}" for s in unit_sessions)
    return hashlib.sha1(ids.encode("utf-8")).hexdigest()[:8]


def _unit_key(source, contributor, group_key, part, unit_sessions):
    return (f"{source.id}/{contributor}/{_group_token(group_key)}/"
            f"part-{part:03d}-{_members_hash(unit_sessions)}.zip")


def _plan_units(sessions):
    """Split a source's sessions into deterministic, size-budgeted units.

    Base unit = one working-dir group; a group over UNIT_BYTES is packed into
    parts of whole sessions (a single oversized session becomes its own part —
    transcripts are never split). Yields (group_key, part_index, [Session]).
    """
    by_group = defaultdict(list)
    for s in sessions:
        by_group[s.group_key].append(s)
    for group_key in sorted(by_group):
        members = sorted(by_group[group_key], key=lambda s: (s.parent or "", s.id))
        part, cur, cur_bytes = 0, [], 0
        for s in members:
            sz = s.size_bytes or 0
            if cur and cur_bytes + sz > UNIT_BYTES:
                yield group_key, part, cur
                part, cur, cur_bytes = part + 1, [], 0
            cur.append(s)
            cur_bytes += sz
        if cur:
            yield group_key, part, cur


def _build_unit_zip(source, unit_sessions, contributor, redact_id=True):
    """Build one unit's zip in memory. Returns (zip_bytes, manifest_dict).

    Secrets/credentials are ALWAYS redacted; identity is optional. Identity
    redaction also covers the archive path and manifest group fields (which
    encode home path / username). Paths come from discovery, never user input.
    """
    buf = io.BytesIO()
    manifest_sessions = []
    total_redactions = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sess in unit_sessions:
            try:
                raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            raw, redaction_count = redact_jsonl_content(raw)   # always — secrets/credentials
            group_key, group_label = sess.group_key, sess.group_label
            if redact_id:
                raw, n = redact_identity(raw); redaction_count += n
                group_key, n = redact_path_token(group_key); redaction_count += n
                group_label, n = redact_path_token(group_label); redaction_count += n
            total_redactions += redaction_count
            if sess.is_subagent and sess.parent:
                arc = f"{group_key}/{sess.parent}/subagents/{sess.id}.jsonl"
            else:
                arc = f"{group_key}/{sess.id}.jsonl"
            zf.writestr(arc, raw)
            manifest_sessions.append({
                "group": group_key, "group_label": group_label, "session": sess.id,
                "is_subagent": sess.is_subagent, "parent": sess.parent,
                "size_bytes": len(raw.encode("utf-8")), "redactions": redaction_count,
            })
        manifest = {
            "source": source.id, "source_format": source.source_format,
            "contributor": contributor, "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "subagent_count": sum(1 for s in manifest_sessions if s["is_subagent"]),
            "sessions": manifest_sessions, "total_redactions": total_redactions,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue(), manifest


def _upload_units(s3, source, sessions, contributor, redact_id=True, on_unit=None):
    """Upload a source's sessions as size-budgeted units.

    Keys are deterministic, so a re-run overwrites the same objects in place
    (idempotent — no duplicates) and an aborted run's completed units stay
    durable. The uploader key needs only s3:PutObject. on_unit(n) ticks progress.
    """
    uploaded = []
    for group_key, part, unit in _plan_units(sessions):
        key = _unit_key(source, contributor, group_key, part, unit)
        zip_bytes, man = _build_unit_zip(source, unit, contributor, redact_id)
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=zip_bytes,
                      ContentType="application/zip")
        uploaded.append({"source": source.id, "s3_key": key,
                         "session_count": len(unit), "zip_size_bytes": len(zip_bytes),
                         "total_redactions": man["total_redactions"]})
        if on_unit:
            on_unit(len(unit))
    return uploaded


# --- background upload jobs (so closing the tab can't abort an upload) ---
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_active_job = {"id": None}


def _resolve_selection(selected):
    """Resolve the UI selection to [(source, [Session])] (no network calls).

    Key on (group, parent, id): subagents share their parent's group, so id
    alone is not unique — must match the archive-path disambiguation.
    """
    picks_by_source: dict[str, set] = defaultdict(set)
    for item in selected:
        picks_by_source[item.get("source", "")].add(
            (item.get("group", ""), item.get("parent") or None, item.get("session", "")))
    out = []
    for source_id, picks in picks_by_source.items():
        source = get_source(source_id)
        if source is None:
            continue
        resolved = {(g.key, s.parent or None, s.id): s
                    for g in source.discover() for s in g.sessions}
        sessions = [resolved[p] for p in picks if p in resolved]
        if sessions:
            out.append((source, sessions))
    return out


def _run_upload_job(job_id, selected, contributor, redact_id):
    """Worker thread: upload all selected sessions as resumable units, ticking
    progress into JOBS[job_id]."""
    job = JOBS[job_id]
    try:
        to_upload = _resolve_selection(selected)
        job["total"] = sum(len(s) for _, s in to_upload)
        job["status"] = "running"
        s3 = _make_s3_client()
        for source, sessions in to_upload:
            try:
                uploaded = _upload_units(
                    s3, source, sessions, contributor, redact_id,
                    on_unit=lambda n: job.__setitem__("done", job["done"] + n))
                with JOBS_LOCK:
                    job["uploads"].extend(uploaded)
            except Exception as e:
                with JOBS_LOCK:
                    job["errors"].append({"source": source.id, "error": f"{type(e).__name__}: {e}"})
        job["status"] = ("completed" if not job["errors"]
                         else "partial" if job["uploads"] else "failed")
    except Exception as e:
        with JOBS_LOCK:
            job["errors"].append({"error": f"{type(e).__name__}: {e}"})
        job["status"] = "failed"
    finally:
        job["finished_at"] = time.time()
        with JOBS_LOCK:
            if _active_job["id"] == job_id:
                _active_job["id"] = None


@app.post("/api/upload")
async def upload(request: Request):
    """Start a background upload job; returns a job id to poll."""
    body = await request.json()
    selected = body.get("selected", [])
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    redact_id = body.get("redact_identity", True)
    if not selected:
        return JSONResponse({"error": "Nothing selected"}, status_code=400)

    with JOBS_LOCK:
        if _active_job["id"] is not None:
            return JSONResponse({"error": "An upload is already running",
                                 "job_id": _active_job["id"]}, status_code=409)
        # Bound memory: drop oldest finished jobs, keep the most recent few.
        finished = [jid for jid, j in JOBS.items() if j["finished_at"] is not None]
        for jid in finished[:-10]:
            JOBS.pop(jid, None)
        job_id = uuid.uuid4().hex[:12]
        _active_job["id"] = job_id
        JOBS[job_id] = {"status": "preparing", "total": None, "done": 0,
                        "errors": [], "uploads": [],
                        "started_at": time.time(), "finished_at": None}

    threading.Thread(target=_run_upload_job,
                     args=(job_id, selected, contributor, redact_id), daemon=True).start()
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/api/upload/{job_id}")
async def upload_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "Unknown job"}, status_code=404)
    with JOBS_LOCK:                       # snapshot — the worker mutates lists concurrently
        snap = dict(job)
        snap["uploads"] = list(job["uploads"])
        snap["errors"] = list(job["errors"])
    return snap


def headless_upload(contributor_name: str = "anonymous"):
    """Upload all transcripts from every source as resumable units, no UI."""
    contributor = _safe_name(contributor_name)
    s3 = _make_s3_client()
    any_found = False

    for source in SOURCES:
        sessions = [s for g in source.discover() for s in g.sessions]
        if not sessions:
            continue
        any_found = True
        print(f"[{source.label}] uploading {len(sessions)} sessions as units...")
        try:
            uploaded = _upload_units(s3, source, sessions, contributor)
        except Exception as e:
            print(f"[{source.label}] upload failed: {type(e).__name__}: {e}")
            continue
        mb = sum(u["zip_size_bytes"] for u in uploaded) / 1024 / 1024
        red = sum(u["total_redactions"] for u in uploaded)
        print(f"[{source.label}] {len(uploaded)} unit(s) uploaded "
              f"({mb:.1f} MB, {red} redactions).")

    print("No transcripts found." if not any_found else "Done!")


def _find_free_port(start: int, host: str = "127.0.0.1", tries: int = 20) -> int | None:
    """Return the first bindable port at or after `start` (scanning `tries`)."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return None


def main():
    headless = "--all" in sys.argv
    contributor_name = "anonymous"
    for i, arg in enumerate(sys.argv):
        if arg == "--name" and i + 1 < len(sys.argv):
            contributor_name = sys.argv[i + 1]

    if headless:
        headless_upload(contributor_name)
    else:
        base = int(os.environ.get("PORT", 8899))
        port = _find_free_port(base)
        if port is None:
            print(f"No free port found in {base}-{base + 19}; is something stuck?")
            return
        if port != base:
            print(f"Port {base} is in use — using {port} instead.")
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        print(f"Opening browser at http://localhost:{port}")
        print("Press Ctrl+C to stop.")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
