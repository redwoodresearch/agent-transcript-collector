"""FastAPI app: local web UI for selecting and uploading agent transcripts.

Supports multiple agent harnesses (Claude Code, Codex, Cursor, Pi) via the source
adapters in `.sources`. Each transcript is uploaded as one ZIP under
``mts-trans/<contributor>/<project>/<source>/<session>/``.
"""

import getpass
import multiprocessing
import os
import queue
import re
import socket
import threading
import time
import uuid
import webbrowser
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, PackageLoader

from .redactor import redact_identity, redact_jsonl_content
from .s3client import make_s3_client as _make_s3_client
from .s3client import selected_profile
from .sources import SOURCES, get_source, projects_from_groups
from .sources.base import human_size, session_sidecars
from .uploader import (
    UploadBusy,
    UploadLock,
    existing_transcripts,
)
from .uploader import (
    upload_transcripts as _upload_transcripts,
)
from .watcher import (
    AllowedGroup,
    WatcherConfig,
    capture_source_env,
)
from .watcher import (
    install as install_watcher,
)
from .watcher import (
    load_config as load_watcher_config,
)
from .watcher import (
    save_config as save_watcher_config,
)
from .watcher import (
    status as watcher_status,
)
from .watcher import (
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
    """Return a local fallback without delaying the first page on a subprocess."""
    return getpass.getuser()


@app.get("/", response_class=HTMLResponse)
def index():
    """Return the application shell immediately; discovery starts in-browser."""
    template = jinja_env.get_template("index.html")
    return template.render(
        projects=None,
        default_contributor_name=_default_contributor_name(),
    )


# One process-local discovery snapshot serves every UI operation. Scanning is
# intentionally background work so the application shell never waits on disk.
SCAN_LOCK = threading.Lock()
SCAN_STATE = {
    "status": "idle",
    "completed_sources": 0,
    "total_sources": len(SOURCES),
    "source": None,
    "session_count": 0,
    "error": None,
    "started_at": None,
    "finished_at": None,
}
SCAN_CACHE = {"projects": None, "groups_by_source": {}, "sessions": {}}


def _scan_status_unlocked() -> dict:
    result = dict(SCAN_STATE)
    result["ready"] = SCAN_CACHE["projects"] is not None
    return result


def _scan_status() -> dict:
    with SCAN_LOCK:
        return _scan_status_unlocked()


def _run_scan() -> None:
    discovered = []
    groups_by_source = {}
    sessions = {}
    try:
        for position, source in enumerate(SOURCES, start=1):
            with SCAN_LOCK:
                SCAN_STATE["source"] = source.label
            groups = source.discover()
            groups_by_source[source.id] = groups
            discovered.extend((source, group) for group in groups)
            for group in groups:
                for session in group.sessions:
                    key = (source.id, group.key, session.parent or None, session.id)
                    sessions[key] = session
            with SCAN_LOCK:
                SCAN_STATE["completed_sources"] = position
                SCAN_STATE["session_count"] = len(sessions)
        projects = projects_from_groups(discovered)
        with SCAN_LOCK:
            SCAN_CACHE.update({
                "projects": projects,
                "groups_by_source": groups_by_source,
                "sessions": sessions,
            })
            SCAN_STATE["status"] = "ready"
            SCAN_STATE["error"] = None
    except Exception as exc:
        with SCAN_LOCK:
            SCAN_STATE["status"] = "failed"
            SCAN_STATE["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with SCAN_LOCK:
            SCAN_STATE["source"] = None
            SCAN_STATE["finished_at"] = time.time()


def _start_scan(force: bool = False) -> dict:
    with SCAN_LOCK:
        if SCAN_STATE["status"] == "scanning":
            return _scan_status_unlocked()
        if SCAN_CACHE["projects"] is not None and not force:
            return _scan_status_unlocked()
        SCAN_STATE.update({
            "status": "scanning",
            "completed_sources": 0,
            "source": None,
            "session_count": 0,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        })
        status = _scan_status_unlocked()
    threading.Thread(target=_run_scan, daemon=True, name="transcript-scan").start()
    return status


def _cached_session(source: str, group: str, session: str, parent: str | None):
    with SCAN_LOCK:
        return SCAN_CACHE["sessions"].get((source, group, parent or None, session))


@app.post("/api/scan")
def start_scan(force: bool = False):
    return _start_scan(force)


@app.get("/api/scan")
def scan_status():
    return _scan_status()


@app.get("/api/projects", response_class=HTMLResponse)
def project_list():
    with SCAN_LOCK:
        projects = SCAN_CACHE["projects"]
    if projects is None:
        return JSONResponse(_scan_status(), status_code=202)
    return jinja_env.get_template("_projects.html").render(projects=projects)


@app.get("/api/preview")
def preview_session(source: str, group: str, session: str, parent: str = "",
                    offset: int = 0, limit: int = 100):
    """Preview one bounded page of messages with displayed text redacted."""
    sess = _cached_session(source, group, session, parent or None)
    src = get_source(source)
    if sess is None or src is None:
        return JSONResponse({"error": "Session not found; try Rescan"}, status_code=404)

    raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")
    # Resolve side files before redaction rewrites their referenced paths.
    sidecars = session_sidecars(src, sess, raw)
    parsed = src.parse_messages(raw)
    offset = max(0, offset)
    limit = max(1, min(limit, 100))
    messages = []
    redaction_count = 0
    for m in parsed[offset:offset + limit]:
        text, count = redact_jsonl_content(m["text"])
        redaction_count += count
        text, count = redact_identity(text)
        redaction_count += count
        messages.append(
            {
                "role": m["role"],
                "text": text[:2000] + ("..." if len(text) > 2000 else ""),
            }
        )

    return {
        "messages": messages,
        "redaction_count": redaction_count,
        "total_messages": len(parsed),
        "next_offset": offset + len(messages),
        "has_more": offset + len(messages) < len(parsed),
        "sidecars": [
            {
                "name": sidecar.arcname,
                "kind": sidecar.kind,
                "size_human": human_size(sidecar.size_bytes),
            }
            for sidecar in sidecars.files
        ],
        "sidecars_missing": len(sidecars.missing),
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
        with SCAN_LOCK:
            resolved = {
                (group, parent, session): value
                for (sid, group, parent, session), value in
                SCAN_CACHE["sessions"].items()
                if sid == source_id
            }
        sessions = [resolved[p] for p in picks if p in resolved]
        if sessions:
            out.append((source, sessions))
    return out


def _upload_worker(to_upload, contributor, events):
    """Run CPU-heavy preparation and uploading outside the web process."""
    uploads = []
    errors = []
    status = "failed"
    try:
        with UploadLock():
            events.put(
                {"type": "progress", "status": "running", "stage": "connecting"}
            )
            s3 = _make_s3_client()
            uploaded_metadata: dict[str, dict] = {}
            for source_id, sessions in to_upload:
                source = get_source(source_id)
                if source is None:
                    errors.append({"source": source_id, "error": "Unknown source"})
                    continue
                try:
                    uploaded, upload_errors = _upload_transcripts(
                        s3,
                        source,
                        sessions,
                        contributor,
                        on_progress=lambda n: events.put(
                            {"type": "advance", "count": n}
                        ),
                        on_status=lambda stage, completed, total: events.put(
                            {
                                "type": "progress",
                                "stage": stage,
                                "stage_done": completed,
                                "stage_total": total,
                            }
                        ),
                        uploaded_metadata=uploaded_metadata,
                    )
                    uploads.extend(uploaded)
                    errors.extend(upload_errors)
                except Exception as e:
                    errors.append(
                        {
                            "source": source.id,
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
            status = (
                "completed"
                if not errors
                else "partial"
                if uploads
                else "failed"
            )
    except UploadBusy as e:
        errors.append({"error": str(e)})
        status = "failed"
    except Exception as e:
        errors.append({"error": f"{type(e).__name__}: {e}"})
        status = "failed"
    finally:
        try:
            events.put(
                {
                    "type": "finished",
                    "status": status,
                    "uploads": uploads,
                    "errors": errors,
                }
            )
        finally:
            # Wait for the child queue's feeder thread to flush the terminal
            # payload before the process exits.
            events.close()
            events.join_thread()


def _monitor_upload_worker(job_id, process, events):
    """Mirror child-process events into the parent process's job registry."""
    finished = False

    def apply_event(event):
        nonlocal finished
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            if event["type"] == "advance":
                job["done"] += event["count"]
            elif event["type"] == "progress":
                for key in ("status", "stage", "stage_done", "stage_total"):
                    if key in event:
                        job[key] = event[key]
            elif event["type"] == "finished":
                job["status"] = event["status"]
                job["uploads"].extend(event["uploads"])
                job["errors"].extend(event["errors"])
                finished = True

    while process.is_alive():
        try:
            event = events.get(timeout=0.25)
        except queue.Empty:
            continue
        apply_event(event)
    process.join()
    # multiprocessing.Queue uses a feeder thread. The child can exit after
    # enqueueing its terminal event but before the parent observes it, so drain
    # everything that was flushed to the pipe before declaring a crash.
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            break
        apply_event(event)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            if not finished:
                job["status"] = "failed"
                job["errors"].append(
                    {"error": f"Upload worker exited unexpectedly ({process.exitcode})"}
                )
            job["finished_at"] = time.time()
        if _active_job["id"] == job_id:
            _active_job["id"] = None
    events.close()


def _upload_history_error(error: str) -> str:
    if "TokenRetrievalError" in error or "token has expired" in error.lower():
        return (
            "AWS login expired. Run: "
            f"aws sso login --profile {selected_profile()}"
        )
    return error


def _uploaded_sessions(body: dict):
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    sessions = body.get("sessions", [])
    by_source: dict[str, list[dict]] = defaultdict(list)
    for item in sessions:
        source_id = item.get("source", "")
        if get_source(source_id) is not None:
            by_source[source_id].append(item)

    try:
        s3 = _make_s3_client()
        uploaded = []
        errors = []
        for source_id, source_sessions in by_source.items():
            source = get_source(source_id)
            with SCAN_LOCK:
                resolved = {
                    (group, parent, session): value
                    for (sid, group, parent, session), value in
                    SCAN_CACHE["sessions"].items()
                    if sid == source_id
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
            found, source_errors = existing_transcripts(
                s3,
                source,
                [session for _, session in resolved_items],
                contributor,
            )
            errors.extend(source_errors)
            found_ids = {
                (session.group_key, session.parent or None, session.id)
                for session in found
            }
            uploaded.extend(
                item
                for item, session in resolved_items
                if (session.group_key, session.parent or None, session.id) in found_ids
            )
            if source_errors:
                break
        if errors:
            # Never turn a failed metadata request into "nothing is uploaded".
            # That false negative both misleads the user and enables needless
            # re-uploads. The browser preserves its last known state on 503.
            first = _upload_history_error(
                errors[0].get("error", "unknown error")
            )
            return JSONResponse(
                {"error": f"Upload history unavailable: {first}"},
                status_code=503,
            )
        return {"uploaded": uploaded}
    except Exception as e:
        error = _upload_history_error(f"{type(e).__name__}: {e}")
        return JSONResponse(
            {"error": f"Upload history unavailable: {error}"},
            status_code=503,
        )


@app.post("/api/uploaded")
async def uploaded_sessions(request: Request):
    """Check remote history in a worker so it never stalls the UI event loop."""
    body = await request.json()
    return await run_in_threadpool(_uploaded_sessions, body)


@app.post("/api/upload")
async def upload(request: Request):
    """Start a background upload job; returns a job id to poll."""
    body = await request.json()
    selected = body.get("selected", [])
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    if not selected:
        return JSONResponse({"error": "Nothing selected"}, status_code=400)
    to_upload = [
        (source.id, sessions) for source, sessions in _resolve_selection(selected)
    ]
    if not to_upload:
        return JSONResponse(
            {"error": "Selected transcripts are no longer available"},
            status_code=400,
        )

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
            "stage": "preparing",
            "stage_done": 0,
            "stage_total": None,
            "total": sum(len(sessions) for _, sessions in to_upload),
            "done": 0,
            "errors": [],
            "uploads": [],
            "started_at": time.time(),
            "finished_at": None,
        }

    try:
        context = multiprocessing.get_context("spawn")
        events = context.Queue()
        process = context.Process(
            target=_upload_worker,
            args=(to_upload, contributor, events),
            daemon=True,
        )
        process.start()
    except Exception as e:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["errors"].append({"error": f"Could not start upload worker: {e}"})
            job["finished_at"] = time.time()
            if _active_job["id"] == job_id:
                _active_job["id"] = None
        return JSONResponse({"error": job["errors"][0]["error"]}, status_code=500)
    threading.Thread(
        target=_monitor_upload_worker,
        args=(job_id, process, events),
        daemon=True,
    ).start()
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/api/watcher")
def get_watcher_status():
    result = watcher_status()
    config = result.get("config")
    if not config:
        return result
    with SCAN_LOCK:
        discovered = {
            (source_id, group.key): group.label
            for source_id, groups in SCAN_CACHE["groups_by_source"].items()
            for group in groups
        }
    configured = {
        (item.get("source", ""), item.get("group", ""))
        for item in config.get("groups", [])
    }
    active_labels = {
        (source, label)
        for (source, group), label in discovered.items()
        if (source, group) in configured
    }
    # Stable project IDs gained a hash during the identity migration. Once the
    # replacement ID is configured, hide its obsolete alias instead of showing
    # a wall of duplicate "unavailable" folders.
    groups = [
        item
        for item in config.get("groups", [])
        if (item.get("source", ""), item.get("group", "")) in discovered
        or (item.get("source", ""), item.get("label", "")) not in active_labels
    ]
    result["ignored_migrated_groups"] = len(config.get("groups", [])) - len(groups)
    config["groups"] = groups
    return result


def _put_watcher(body: dict):
    requested = {
        (str(item.get("source", "")), str(item.get("group", "")))
        for item in body.get("groups", [])
    }
    removed = {
        (str(item.get("source", "")), str(item.get("group", "")))
        for item in body.get("removed_groups", [])
    }
    requested -= removed
    with SCAN_LOCK:
        discovered = {
            (source_id, group.key): group.label
            for source_id, groups in SCAN_CACHE["groups_by_source"].items()
            for group in groups
        }
    invalid = sorted(requested - discovered.keys())
    if invalid:
        return JSONResponse(
            {"error": "One or more selected folders are no longer available"},
            status_code=400,
        )
    try:
        watcher = watcher_status()
        existing = load_watcher_config() if watcher.get("config") else None
        groups = [
            AllowedGroup(source=source, group=group, label=discovered[(source, group)])
            for source, group in sorted(requested)
        ]
        if existing:
            requested_labels = {
                (source, discovered[(source, group)]) for source, group in requested
            }
            groups.extend(
                group
                for group in existing.groups
                if (group.source, group.group) not in discovered
                and (group.source, group.group) not in removed
                and (group.source, group.label) not in requested_labels
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
            source_env=capture_source_env(),
            package_spec=WatcherConfig.package_spec,
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


@app.put("/api/watcher")
async def put_watcher(request: Request):
    """Persist project consent without blocking unrelated UI requests."""
    body = await request.json()
    return await run_in_threadpool(_put_watcher, body)


@app.post("/api/watcher/reinstall")
def reinstall_watcher():
    """Reinstall the configured auto uploader with the current version."""
    try:
        watcher = watcher_status()
        if not watcher.get("configured"):
            return JSONResponse(
                {"error": "Configure auto upload before reinstalling it"},
                status_code=400,
            )
        config = load_watcher_config()
        config.source_env = capture_source_env()
        return install_watcher(config)
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not reinstall auto upload: {type(e).__name__}: {e}"},
            status_code=500,
        )


@app.delete("/api/watcher")
def delete_watcher():
    try:
        return uninstall_watcher()
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not uninstall watcher: {type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/api/upload/{job_id}")
def upload_status(job_id: str):
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
    base = int(os.environ.get("PORT", "8899"))
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
