"""FastAPI app: local web UI for selecting and uploading agent transcripts.

Supports multiple agent harnesses (Claude Code, Codex, Cursor, Pi) via the source
adapters in `.sources`. Each transcript version is uploaded as one ZIP under
``mts-trans/<contributor>/<project>/<source>/<session>/``.
"""

import getpass
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, PackageLoader

from .redactor import redact_identity, redact_jsonl_content
from .s3client import make_s3_client as _make_s3_client, selected_profile
from .sources import SOURCES, detect_projects, find_session, get_source
from .uploader import (
    UploadBusy,
    UploadLock,
    list_uploaded_keys,
    partition_transcripts,
    upload_transcripts as _upload_transcripts,
)
from .watcher import (
    AllowedGroup,
    WatcherConfig,
    capture_source_env,
    install as install_watcher,
    load_config as load_watcher_config,
    save_config as save_watcher_config,
    status as watcher_status,
    uninstall as uninstall_watcher,
)


def _safe_name(name: str) -> str:
    """Sanitize a contributor name for use as an S3 key segment."""
    name = (name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return name or "anonymous"


app = FastAPI()

jinja_env = Environment(
    loader=PackageLoader("agent_transcript_collector", "templates"),
    autoescape=True,
)


@lru_cache(maxsize=1)
def _default_contributor_name() -> str:
    """Prefer the authenticated GitHub login, then the local OS username."""
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "api", "user", "--jq", ".login"],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            )
            login = result.stdout.strip()
            if login:
                return login
        except (OSError, subprocess.SubprocessError):
            pass
    return getpass.getuser()


@app.get("/", response_class=HTMLResponse)
async def index():
    projects = detect_projects()
    template = jinja_env.get_template("index.html")
    return template.render(
        projects=projects,
        default_contributor_name=_default_contributor_name(),
    )


@app.get("/api/preview")
async def preview_session(source: str, group: str, session: str, parent: str = ""):
    """Preview a session's messages with secrets and identity redacted."""
    sess = find_session(source, group, session, parent or None)
    src = get_source(source)
    if sess is None or src is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")

    raw, redaction_count = redact_jsonl_content(raw)
    raw, n = redact_identity(raw)
    redaction_count += n

    messages = []
    for m in src.parse_messages(raw):
        text = m["text"]
        messages.append(
            {
                "role": m["role"],
                "text": text[:2000] + ("..." if len(text) > 2000 else ""),
            }
        )

    return {
        "messages": messages,
        "redaction_count": redaction_count,
        "total_messages": len(messages),
    }


# --- background upload jobs (so closing the tab can't abort an upload) ---
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_active_job = {"id": None}


def _resolve_selection(selected):
    """Resolve the UI selection to [(source, [Session])] (no network calls).

    Key on (group, parent, id): subagents share their parent's group, so id
    alone is not unique, so parent identity is part of the selection key.
    """
    picks_by_source: dict[str, set] = defaultdict(set)
    for item in selected:
        picks_by_source[item.get("source", "")].add(
            (item.get("group", ""), item.get("parent") or None, item.get("session", ""))
        )
    out = []
    for source_id, picks in picks_by_source.items():
        source = get_source(source_id)
        if source is None:
            continue
        resolved = {
            (g.key, s.parent or None, s.id): s
            for g in source.discover()
            for s in g.sessions
        }
        sessions = [resolved[p] for p in picks if p in resolved]
        if sessions:
            out.append((source, sessions))
    return out


def _run_upload_job(job_id, selected, contributor):
    """Upload selected transcript versions while updating job progress."""
    job = JOBS[job_id]
    try:
        with UploadLock():
            to_upload = _resolve_selection(selected)
            job["total"] = sum(len(s) for _, s in to_upload)
            job["status"] = "running"
            s3 = _make_s3_client()
            uploaded_keys = list_uploaded_keys(s3, contributor)
            for source, sessions in to_upload:
                try:
                    uploaded, upload_errors = _upload_transcripts(
                        s3,
                        source,
                        sessions,
                        contributor,
                        on_progress=lambda n: job.__setitem__("done", job["done"] + n),
                        uploaded_keys=uploaded_keys,
                    )
                    with JOBS_LOCK:
                        job["uploads"].extend(uploaded)
                        job["errors"].extend(upload_errors)
                except Exception as e:
                    with JOBS_LOCK:
                        job["errors"].append(
                            {
                                "source": source.id,
                                "error": f"{type(e).__name__}: {e}",
                            }
                        )
            job["status"] = (
                "completed"
                if not job["errors"]
                else "partial"
                if job["uploads"]
                else "failed"
            )
    except UploadBusy as e:
        with JOBS_LOCK:
            job["errors"].append({"error": str(e)})
        job["status"] = "failed"
    except Exception as e:
        with JOBS_LOCK:
            job["errors"].append({"error": f"{type(e).__name__}: {e}"})
        job["status"] = "failed"
    finally:
        job["finished_at"] = time.time()
        with JOBS_LOCK:
            if _active_job["id"] == job_id:
                _active_job["id"] = None


@app.post("/api/uploaded")
async def uploaded_sessions(request: Request):
    """Return local sessions whose current transcript version is in S3."""
    body = await request.json()
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    sessions = body.get("sessions", [])
    by_source: dict[str, list[dict]] = defaultdict(list)
    for item in sessions:
        source_id = item.get("source", "")
        if get_source(source_id) is not None:
            by_source[source_id].append(item)

    try:
        s3 = _make_s3_client()
        uploaded_keys = list_uploaded_keys(s3, contributor)
        uploaded = []
        for source_id, source_sessions in by_source.items():
            source = get_source(source_id)
            resolved = {
                (group.key, session.parent or None, session.id): session
                for group in source.discover()
                for session in group.sessions
            }
            resolved_items = []
            for item in source_sessions:
                session = resolved.get(
                    (
                        item.get("group", ""),
                        item.get("parent") or None,
                        item.get("session", ""),
                    )
                )
                if session is not None:
                    resolved_items.append((item, session))
            _, found, _ = partition_transcripts(
                s3,
                source,
                [session for _, session in resolved_items],
                contributor,
                uploaded_keys,
            )
            found_ids = {
                (session.group_key, session.parent or None, session.id)
                for session in found
            }
            uploaded.extend(
                item
                for item, session in resolved_items
                if (session.group_key, session.parent or None, session.id) in found_ids
            )
        return {"uploaded": uploaded}
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not check upload history: {type(e).__name__}: {e}"},
            status_code=502,
        )


@app.post("/api/upload")
async def upload(request: Request):
    """Start a background upload job; returns a job id to poll."""
    body = await request.json()
    selected = body.get("selected", [])
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    if not selected:
        return JSONResponse({"error": "Nothing selected"}, status_code=400)

    with JOBS_LOCK:
        if _active_job["id"] is not None:
            return JSONResponse(
                {"error": "An upload is already running", "job_id": _active_job["id"]},
                status_code=409,
            )
        # Bound memory: drop oldest finished jobs, keep the most recent few.
        finished = [jid for jid, j in JOBS.items() if j["finished_at"] is not None]
        for jid in finished[:-10]:
            JOBS.pop(jid, None)
        job_id = uuid.uuid4().hex[:12]
        _active_job["id"] = job_id
        JOBS[job_id] = {
            "status": "preparing",
            "total": None,
            "done": 0,
            "errors": [],
            "uploads": [],
            "started_at": time.time(),
            "finished_at": None,
        }

    threading.Thread(
        target=_run_upload_job,
        args=(job_id, selected, contributor),
        daemon=True,
    ).start()
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/api/watcher")
async def get_watcher_status():
    return watcher_status()


@app.put("/api/watcher")
async def put_watcher(request: Request):
    """Persist project consent and optionally enable the scheduled job."""
    body = await request.json()
    requested = {
        (str(item.get("source", "")), str(item.get("group", "")))
        for item in body.get("groups", [])
    }
    discovered = {
        (source.id, group.key): group.label
        for source in SOURCES
        for group in source.discover()
    }
    invalid = sorted(requested - discovered.keys())
    if invalid:
        return JSONResponse(
            {"error": "One or more selected folders are no longer available"},
            status_code=400,
        )
    try:
        watcher = watcher_status()
        existing = load_watcher_config() if watcher.get("configured") else None
        groups = [
            AllowedGroup(source=source, group=group, label=discovered[(source, group)])
            for source, group in sorted(requested)
        ]
        if existing:
            groups.extend(
                group
                for group in existing.groups
                if (group.source, group.group) not in discovered
            )
        config = WatcherConfig(
            auto_uploader_version=(
                existing.auto_uploader_version
                if existing
                else WatcherConfig.auto_uploader_version
            ),
            contributor=_safe_name(body.get("contributor_name", "anonymous")),
            aws_profile=existing.aws_profile if existing else selected_profile(),
            groups=groups,
            source_env=existing.source_env if existing else capture_source_env(),
            package_spec=(
                existing.package_spec if existing else WatcherConfig.package_spec
            ),
            uv_path=existing.uv_path if existing else "",
        )
        enabled = bool(body.get("enabled", True))
        installed = bool(watcher.get("installed"))
        if enabled and not installed:
            return install_watcher(config)
        path = save_watcher_config(config)
        return {
            "installed": installed,
            "configured": True,
            "config_path": str(path),
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not update auto upload: {type(e).__name__}: {e}"},
            status_code=500,
        )


@app.post("/api/watcher/reinstall")
async def reinstall_watcher():
    """Reinstall the configured auto uploader with the current version."""
    try:
        watcher = watcher_status()
        if not watcher.get("configured"):
            return JSONResponse(
                {"error": "Configure auto upload before reinstalling it"},
                status_code=400,
            )
        return install_watcher(load_watcher_config())
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not reinstall auto upload: {type(e).__name__}: {e}"},
            status_code=500,
        )


@app.delete("/api/watcher")
async def delete_watcher():
    try:
        return uninstall_watcher()
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not uninstall watcher: {type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/api/upload/{job_id}")
async def upload_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "Unknown job"}, status_code=404)
    with JOBS_LOCK:  # snapshot — the worker mutates lists concurrently
        snap = dict(job)
        snap["uploads"] = list(job["uploads"])
        snap["errors"] = list(job["errors"])
    return snap


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


def main() -> int:
    base = int(os.environ.get("PORT", 8899))
    port = _find_free_port(base)
    if port is None:
        print(f"No free port found in {base}-{base + 19}; is something stuck?")
        return 1
    if port != base:
        print(f"Port {base} is in use — using {port} instead.")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print(f"Opening browser at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
