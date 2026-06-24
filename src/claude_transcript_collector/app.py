"""FastAPI app: local web UI for selecting and uploading agent transcripts.

Supports multiple agent harnesses (Claude Code, Codex, Pi) via the source
adapters in `.sources`. Each upload produces one zip per source, stored under a
source-first S3 key: <bucket>/<source>/<contributor>/<timestamp>-<hex>.zip
"""

import io
import json
import os
import re
import sys
import uuid
import webbrowser
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from threading import Timer

import boto3
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, PackageLoader

from .redactor import redact_jsonl_content
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
async def preview_session(source: str, group: str, session: str, redact: bool = True):
    """Preview a session's messages, optionally redacted."""
    sess = find_session(source, group, session)
    src = get_source(source)
    if sess is None or src is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")

    redaction_count = 0
    if redact:
        raw, redaction_count = redact_jsonl_content(raw)

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


def _zip_and_upload(s3, source, sessions, contributor, redact_secrets):
    """Zip one source's sessions (with redaction) and upload to S3.

    Returns a per-source result dict. Sessions are pre-resolved Session objects,
    so the file paths come from discovery, never from user input.
    """
    buf = io.BytesIO()
    manifest_sessions = []
    total_redactions = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sess in sessions:
            try:
                raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            redaction_count = 0
            if redact_secrets:
                raw, redaction_count = redact_jsonl_content(raw)
                total_redactions += redaction_count

            zf.writestr(f"{sess.group_key}/{sess.id}.jsonl", raw)
            manifest_sessions.append({
                "group": sess.group_key,
                "group_label": sess.group_label,
                "session": sess.id,
                "size_bytes": len(raw.encode("utf-8")),
                "redactions": redaction_count,
            })

        zf.writestr("manifest.json", json.dumps({
            "source": source.id,
            "source_format": source.source_format,
            "contributor": contributor,
            "uploaded_at": datetime.utcnow().isoformat(),
            "sessions": manifest_sessions,
            "total_redactions": total_redactions,
        }, indent=2))

    zip_bytes = buf.getvalue()
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    s3_key = f"{source.id}/{contributor}/{timestamp}-{uuid.uuid4().hex[:8]}.zip"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=zip_bytes,
        ContentType="application/zip",
    )

    return {
        "source": source.id,
        "s3_key": s3_key,
        "zip_size_bytes": len(zip_bytes),
        "session_count": len(manifest_sessions),
        "total_redactions": total_redactions,
    }


@app.post("/api/upload")
async def upload(request: Request):
    """Zip selected sessions per source and upload one zip per source to S3."""
    body = await request.json()
    selected = body.get("selected", [])
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    redact_secrets = body.get("redact_secrets", True)

    if not selected:
        return JSONResponse({"error": "Nothing selected"}, status_code=400)

    # Group selections by source, then resolve each to a discovered Session.
    picks_by_source: dict[str, set] = defaultdict(set)
    for item in selected:
        picks_by_source[item.get("source", "")].add(
            (item.get("group", ""), item.get("session", ""))
        )

    # Resolve selections to discovered sessions first (no network calls).
    to_upload = []  # list of (source, [Session])
    for source_id, picks in picks_by_source.items():
        source = get_source(source_id)
        if source is None:
            continue
        resolved = {
            (g.key, s.id): s
            for g in source.discover()
            for s in g.sessions
        }
        sessions = [resolved[p] for p in picks if p in resolved]
        if sessions:
            to_upload.append((source, sessions))

    if not to_upload:
        return JSONResponse({"error": "No matching sessions found"}, status_code=400)

    # Upload per source so one source's failure doesn't discard the others'
    # already-built uploads (mirrors headless_upload's per-source handling).
    s3 = _make_s3_client()
    uploads = []
    errors = []
    for source, sessions in to_upload:
        try:
            uploads.append(_zip_and_upload(s3, source, sessions, contributor, redact_secrets))
        except Exception as e:
            errors.append({"source": source.id, "error": f"{type(e).__name__}: {e}"})

    if not uploads:
        return JSONResponse({"error": "Upload failed", "errors": errors}, status_code=502)

    return {
        "status": "uploaded" if not errors else "partial",
        "uploads": uploads,
        "errors": errors,
        "session_count": sum(u["session_count"] for u in uploads),
        "zip_size_bytes": sum(u["zip_size_bytes"] for u in uploads),
        "total_redactions": sum(u["total_redactions"] for u in uploads),
    }


def headless_upload(contributor_name: str = "anonymous"):
    """Upload all transcripts from every detected source immediately, no UI."""
    contributor = _safe_name(contributor_name)
    s3 = _make_s3_client()
    any_uploaded = False

    for source in SOURCES:
        sessions = [s for g in source.discover() for s in g.sessions]
        if not sessions:
            continue
        any_uploaded = True
        print(f"[{source.label}] redacting and zipping {len(sessions)} sessions...")
        try:
            res = _zip_and_upload(s3, source, sessions, contributor, redact_secrets=True)
        except Exception as e:
            print(f"[{source.label}] upload failed: {type(e).__name__}: {e}")
            continue
        print(
            f"[{source.label}] uploaded {res['session_count']} sessions "
            f"({res['zip_size_bytes'] / 1024 / 1024:.1f} MB, "
            f"{res['total_redactions']} secrets redacted) -> {res['s3_key']}"
        )

    if not any_uploaded:
        print("No transcripts found.")
    else:
        print("Done!")


def main():
    headless = "--all" in sys.argv
    contributor_name = "anonymous"
    for i, arg in enumerate(sys.argv):
        if arg == "--name" and i + 1 < len(sys.argv):
            contributor_name = sys.argv[i + 1]

    if headless:
        headless_upload(contributor_name)
    else:
        port = int(os.environ.get("PORT", 8899))
        Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        print(f"Opening browser at http://localhost:{port}")
        print("Press Ctrl+C to stop.")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
