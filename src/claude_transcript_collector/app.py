"""FastAPI app: local web UI for selecting and uploading Claude Code transcripts."""

import io
import json
import os
import sys
import uuid
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Timer

import boto3
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, PackageLoader

from .redactor import redact_jsonl_content
from .scanner import scan_projects

S3_BUCKET = os.environ.get("CTC_S3_BUCKET", "claude-transcripts-myles")
S3_REGION = os.environ.get("CTC_S3_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.environ.get("CTC_AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("CTC_AWS_SECRET_ACCESS_KEY", "")

app = FastAPI()

jinja_env = Environment(
    loader=PackageLoader("claude_transcript_collector", "templates"),
    autoescape=True,
)


@app.get("/", response_class=HTMLResponse)
async def index():
    projects = scan_projects()
    template = jinja_env.get_template("index.html")
    return template.render(projects=projects)


@app.get("/api/preview/{project_name}/{session_id}")
async def preview_session(project_name: str, session_id: str, redact: bool = True):
    """Preview a session's messages, optionally redacted."""
    from .scanner import get_projects_dir

    session_path = get_projects_dir() / project_name / f"{session_id}.jsonl"
    if not session_path.exists():
        return JSONResponse({"error": "Session not found"}, status_code=404)

    raw = session_path.read_text(encoding="utf-8", errors="replace")

    redaction_count = 0
    if redact:
        raw, redaction_count = redact_jsonl_content(raw)

    messages = []
    for line in raw.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") in ("user", "assistant"):
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            texts.append(f"[Tool: {block.get('name', '?')}]")
                        elif block.get("type") == "tool_result":
                            texts.append("[Tool Result]")
                text = "\n".join(texts)
            else:
                text = str(content)
            messages.append({
                "role": entry["type"],
                "text": text[:2000] + ("..." if len(text) > 2000 else ""),
            })

    return {
        "messages": messages,
        "redaction_count": redaction_count,
        "total_messages": len(messages),
    }


@app.post("/api/upload")
async def upload(request: Request):
    """Zip selected sessions (with redaction) and upload to S3."""
    body = await request.json()
    selected = body.get("selected", [])
    contributor_name = body.get("contributor_name", "anonymous")
    redact_secrets = body.get("redact_secrets", True)

    if not selected:
        return JSONResponse({"error": "Nothing selected"}, status_code=400)

    from .scanner import get_projects_dir
    projects_dir = get_projects_dir()

    buf = io.BytesIO()
    manifest = []
    total_redactions = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in selected:
            project_name = item["project"]
            session_id = item["session"]
            session_path = projects_dir / project_name / f"{session_id}.jsonl"

            if not session_path.exists():
                continue

            raw = session_path.read_text(encoding="utf-8", errors="replace")
            redaction_count = 0
            if redact_secrets:
                raw, redaction_count = redact_jsonl_content(raw)
                total_redactions += redaction_count

            archive_path = f"{project_name}/{session_id}.jsonl"
            zf.writestr(archive_path, raw)
            manifest.append({
                "project": project_name,
                "session": session_id,
                "size_bytes": len(raw.encode("utf-8")),
                "redactions": redaction_count,
            })

        zf.writestr("manifest.json", json.dumps({
            "contributor": contributor_name,
            "uploaded_at": datetime.utcnow().isoformat(),
            "sessions": manifest,
            "total_redactions": total_redactions,
        }, indent=2))

    zip_bytes = buf.getvalue()
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    s3_key = f"{contributor_name}/{timestamp}-{uuid.uuid4().hex[:8]}.zip"

    s3 = boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=zip_bytes,
        ContentType="application/zip",
    )

    return {
        "status": "uploaded",
        "s3_key": s3_key,
        "zip_size_bytes": len(zip_bytes),
        "session_count": len(manifest),
        "total_redactions": total_redactions,
    }


def headless_upload(contributor_name: str = "anonymous"):
    """Upload all transcripts immediately without UI."""
    from .scanner import scan_projects, get_projects_dir

    projects = scan_projects()
    if not projects:
        print("No transcripts found.")
        return

    projects_dir = get_projects_dir()
    buf = io.BytesIO()
    manifest = []
    total_redactions = 0
    total_sessions = 0

    print(f"Found {sum(p['session_count'] for p in projects)} sessions across {len(projects)} projects.")
    print("Redacting secrets and zipping...")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for project in projects:
            for session in project["sessions"]:
                session_path = projects_dir / project["encoded_name"] / f"{session['id']}.jsonl"
                if not session_path.exists():
                    continue

                raw = session_path.read_text(encoding="utf-8", errors="replace")
                raw, redaction_count = redact_jsonl_content(raw)
                total_redactions += redaction_count
                total_sessions += 1

                archive_path = f"{project['encoded_name']}/{session['id']}.jsonl"
                zf.writestr(archive_path, raw)
                manifest.append({
                    "project": project["encoded_name"],
                    "session": session["id"],
                    "size_bytes": len(raw.encode("utf-8")),
                    "redactions": redaction_count,
                })

        zf.writestr("manifest.json", json.dumps({
            "contributor": contributor_name,
            "uploaded_at": datetime.utcnow().isoformat(),
            "sessions": manifest,
            "total_redactions": total_redactions,
        }, indent=2))

    zip_bytes = buf.getvalue()
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    s3_key = f"{contributor_name}/{timestamp}-{uuid.uuid4().hex[:8]}.zip"

    print(f"Uploading {total_sessions} sessions ({len(zip_bytes) / 1024 / 1024:.1f} MB, {total_redactions} secrets redacted)...")

    s3 = boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=zip_bytes,
        ContentType="application/zip",
    )

    print(f"Done! Uploaded to {s3_key}")


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
