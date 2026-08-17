"""FastAPI app: local web UI for selecting and uploading agent transcripts.

Supports multiple agent harnesses (Claude Code, Codex, Cursor, Pi) via the source
adapters in `.sources`. Each transcript is uploaded as one ZIP under
``mts-trans/<contributor>/<project>/<transcript-id>.zip``.
"""

import getpass
import multiprocessing
import os
import queue
import re
import socket
import tempfile
import threading
import time
import uuid
import webbrowser
from functools import lru_cache

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, PackageLoader

from .redactor import redact_identity, redact_jsonl_content
from .s3client import make_s3_client as _make_s3_client
from .s3client import selected_profile
from .scan import ScanResult, load_transcript_inputs, scan_transcripts
from .sources import SOURCES, get_source
from .sources.base import human_size
from .upload_status import refresh_upload_status
from .upload_workflow import prepare_uploads, record_uploaded, upload_candidates
from .uploader import (
    UploadBusy,
    UploadLock,
    upload_artifacts,
)
from .watcher import (
    AllowedProject,
    WatcherConfig,
    capture_source_env,
    selected_project_identities,
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
SCAN_RESULT: ScanResult | None = None


def _scan_status_unlocked() -> dict:
    result = dict(SCAN_STATE)
    result["ready"] = SCAN_RESULT is not None
    return result


def _scan_status() -> dict:
    with SCAN_LOCK:
        return _scan_status_unlocked()


def _run_scan() -> None:
    global SCAN_RESULT

    try:
        def progress(position, total, source, session_count):
            with SCAN_LOCK:
                SCAN_STATE.update({
                    "source": source.label,
                    "completed_sources": position,
                    "total_sources": total,
                    "session_count": session_count,
                })

        result = scan_transcripts(on_progress=progress)
        with SCAN_LOCK:
            SCAN_RESULT = result
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
        if (
            SCAN_RESULT is not None
            and SCAN_STATE["status"] == "ready"
            and not force
        ):
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


def _cached_session(source: str, project: str, session: str, parent: str | None):
    with SCAN_LOCK:
        result = SCAN_RESULT
    return result.find_session(source, project, session, parent) if result else None


@app.post("/api/scan")
def start_scan(force: bool = False):
    return _start_scan(force)


@app.get("/api/scan")
def scan_status():
    return _scan_status()


@app.get("/api/projects", response_class=HTMLResponse)
def project_list():
    with SCAN_LOCK:
        result = SCAN_RESULT
    if result is None:
        return JSONResponse(_scan_status(), status_code=202)
    return jinja_env.get_template("_projects.html").render(
        projects=result.project_dicts
    )


@app.get("/api/preview")
def preview_session(source: str, project: str, session: str, parent: str = "",
                    offset: int = 0, limit: int = 100):
    """Preview one bounded page of messages with displayed text redacted."""
    sess = _cached_session(source, project, session, parent or None)
    src = get_source(source)
    if sess is None or src is None:
        return JSONResponse({"error": "Session not found; try Refresh"}, status_code=404)

    try:
        inputs = load_transcript_inputs(src, sess)
    except OSError:
        return JSONResponse(
            {"error": "Session file is no longer available; try Refresh"},
            status_code=404,
        )
    # Resolve attachments before redaction rewrites their referenced paths.
    attachments = inputs.attachments
    parsed = src.parse_messages(inputs.text)
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
        "attachments": [
            {
                "name": attachment.arcname,
                "kind": attachment.kind,
                "size_human": human_size(attachment.size_bytes),
            }
            for attachment in attachments.files
        ],
        "attachments_missing": len(attachments.missing),
    }


# --- one background scan → hash changed → check changed pipeline ---
PIPELINE_RUNS: dict[str, dict] = {}
PIPELINE_LOCK = threading.Lock()
_active_pipeline: dict[str, str | None] = {"id": None}


def _pipeline_worker(serialized_selections, contributor, events):
    selections = [
        (source, sessions)
        for source_id, sessions in serialized_selections
        if (source := get_source(source_id)) is not None
    ]
    try:
        with UploadLock():
            def progress(stage, done, total):
                events.put({
                    "type": "progress", "stage": stage,
                    "done": done, "total": total,
                })

            result = refresh_upload_status(
                selections, contributor, on_progress=progress
            )
    except Exception as exc:
        result = {
            "status": "failed", "items": [],
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            "total": sum(len(sessions) for _, sessions in selections),
            "changed": 0, "checked": 0, "cached": 0,
        }
    try:
        events.put({"type": "finished", **result})
    finally:
        events.close()
        events.join_thread()


def _monitor_pipeline(run_id, process, events):
    finished = False

    def apply(event):
        nonlocal finished
        with PIPELINE_LOCK:
            run = PIPELINE_RUNS.get(run_id)
            if run is None:
                return
            if event["type"] == "progress":
                run.update(stage=event["stage"], done=event["done"],
                           work_total=event["total"])
            else:
                for key in ("status", "items", "errors", "total", "changed",
                            "checked", "cached"):
                    run[key] = event[key]
                run["finished_at"] = time.time()
                finished = True

    while process.is_alive():
        try:
            apply(events.get(timeout=0.25))
        except queue.Empty:
            continue
    process.join()
    while True:
        try:
            apply(events.get_nowait())
        except queue.Empty:
            break
    with PIPELINE_LOCK:
        run = PIPELINE_RUNS.get(run_id)
        if run is not None and not finished:
            run["status"] = "failed"
            run["errors"] = [{
                "error": f"Pipeline worker exited unexpectedly ({process.exitcode})"
            }]
            run["finished_at"] = time.time()
        if _active_pipeline["id"] == run_id:
            _active_pipeline["id"] = None
    events.close()


def _start_pipeline(body: dict):
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    selections = _resolve_selection(body.get("sessions", []))
    if not selections:
        return JSONResponse({"error": "No transcripts are available"}, status_code=400)
    with PIPELINE_LOCK:
        if _active_pipeline["id"] is not None:
            active_id = _active_pipeline["id"]
            active = PIPELINE_RUNS[active_id]
            if active["finished_at"] is None:
                if active["contributor"] == contributor:
                    return {"run_id": active_id}
                # Let the browser follow the active run, then start the requested
                # contributor's refresh as soon as the shared worker is free.
                return JSONResponse(
                    {"run_id": active_id,
                     "waiting_for_contributor": contributor},
                    status_code=202,
                )
            _active_pipeline["id"] = None
        finished = [rid for rid, run in PIPELINE_RUNS.items()
                    if run["finished_at"] is not None]
        for rid in finished[:-5]:
            PIPELINE_RUNS.pop(rid, None)
        run_id = uuid.uuid4().hex[:12]
        total = sum(len(sessions) for _, sessions in selections)
        PIPELINE_RUNS[run_id] = {
            "status": "running", "stage": "checking", "done": 0,
            "work_total": 0, "total": total, "changed": 0, "checked": 0,
            "cached": 0, "items": [], "errors": [],
            "contributor": contributor,
            "started_at": time.time(), "finished_at": None,
        }
        _active_pipeline["id"] = run_id
    try:
        context = multiprocessing.get_context("spawn")
        events = context.Queue()
        process = context.Process(
            target=_pipeline_worker,
            args=([(source.id, sessions) for source, sessions in selections],
                  contributor, events), daemon=True,
        )
        process.start()
    except Exception as exc:
        with PIPELINE_LOCK:
            run = PIPELINE_RUNS[run_id]
            run["status"] = "failed"
            run["errors"] = [{"error": f"Could not start pipeline: {exc}"}]
            run["finished_at"] = time.time()
            _active_pipeline["id"] = None
        return JSONResponse({"error": run["errors"][0]["error"]}, status_code=500)
    threading.Thread(
        target=_monitor_pipeline, args=(run_id, process, events), daemon=True
    ).start()
    return JSONResponse({"run_id": run_id}, status_code=202)


@app.post("/api/pipeline")
async def start_pipeline(request: Request):
    return await run_in_threadpool(_start_pipeline, await request.json())


@app.get("/api/pipeline/{run_id}")
def pipeline_status(run_id: str):
    with PIPELINE_LOCK:
        run = PIPELINE_RUNS.get(run_id)
        if run is None:
            return JSONResponse({"error": "Unknown pipeline run"}, status_code=404)
        snapshot = dict(run)
        snapshot["items"] = list(run["items"])
        snapshot["errors"] = list(run["errors"])
        return snapshot


# --- background upload jobs (so closing the tab can't abort an upload) ---
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_active_job: dict[str, str | None] = {"id": None}


def _resolve_selection(selected):
    """Resolve untrusted UI descriptors against the current scan."""
    with SCAN_LOCK:
        result = SCAN_RESULT
    return result.resolve_sessions(selected) if result else []


def _upload_worker(serialized_selections, candidates, contributor, events):
    """Redact, package, and upload candidates confirmed by Refresh."""
    uploads = []
    errors = []
    status = "failed"
    try:
        with UploadLock():
            selections = [
                (source, sessions)
                for source_id, sessions in serialized_selections
                if (source := get_source(source_id)) is not None
            ]

            def progress(stage, done, total):
                events.put({
                    "type": "progress", "status": "running", "stage": stage,
                    "stage_done": done, "stage_total": total,
                    "stage_item_done": done, "stage_item_total": total,
                })

            with tempfile.TemporaryDirectory(prefix="ctc-upload-") as directory:
                progress("redacting", 0, len(candidates))
                artifacts, preparation_errors = prepare_uploads(
                    selections,
                    candidates,
                    contributor,
                    directory,
                    on_progress=lambda done, total:
                        progress("redacting", done, total),
                )
                errors.extend(preparation_errors)
                completed = 0

                def advance(count):
                    nonlocal completed
                    completed += count
                    events.put({"type": "advance", "count": count})
                    progress("uploading", completed, len(artifacts))

                progress("uploading", 0, len(artifacts))
                uploads, upload_errors = upload_artifacts(
                    _make_s3_client(), artifacts, on_progress=advance
                )
                errors.extend(upload_errors)
            successful = {
                (item.get("source"), item.get("project"), item.get("parent") or "",
                 item.get("session"))
                for item in uploads
            }
            uploaded_artifacts = [
                item for item in artifacts
                if (item.get("source"), item.get("project"),
                    item.get("parent") or "", item.get("session")) in successful
            ]
            if uploaded_artifacts:
                record_uploaded(uploaded_artifacts, contributor)
            status = "completed" if not errors else "partial" if uploads else "failed"
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
                for key in (
                    "status",
                    "stage",
                    "stage_done",
                    "stage_total",
                    "stage_item_done",
                    "stage_item_total",
                ):
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


@app.post("/api/upload")
async def upload(request: Request):
    """Start a background upload job; returns a job id to poll."""
    body = await request.json()
    selected = body.get("selected", [])
    contributor = _safe_name(body.get("contributor_name", "anonymous"))
    if not selected:
        return JSONResponse({"error": "Nothing selected"}, status_code=400)
    selections = _resolve_selection(selected)
    if not selections:
        return JSONResponse(
            {"error": "Selected transcripts are no longer available"},
            status_code=400,
        )
    candidates, stale = upload_candidates(selections, contributor)
    if stale:
        return JSONResponse(
            {"error": "Upload status is stale. Refresh before uploading."},
            status_code=409,
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
            "status": "uploading",
            "stage": "redacting",
            "stage_done": 0,
            "stage_total": len(candidates),
            "total": len(candidates),
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
            args=([(source.id, sessions) for source, sessions in selections],
                  candidates, contributor, events),
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
        scan = SCAN_RESULT
    projects = scan.project_dicts if scan else []
    visible_projects = {project["identity"]: project for project in projects}
    saved_config = load_watcher_config()
    # Report the explicit per-project consent only. Expanding this to every
    # project while all_projects is on would make the UI check every box, and
    # the next save would persist them as individual consent that outlives the
    # machine-wide override.
    selected_ids = {
        project.identity for project in saved_config.projects
    } & visible_projects.keys()
    selected = [
        {"identity": identity, "label": visible_projects[identity]["label"]}
        for identity in selected_ids
    ]
    selected.extend(
        {"identity": saved.identity, "label": saved.label}
        for saved in saved_config.projects
        if saved.identity not in visible_projects
    )
    config["projects"] = list({
        item.get("identity", ""): item for item in selected
    }.values())
    return result


def _put_watcher(body: dict):
    requested_ids = {
        str(item.get("identity", "")) for item in body.get("projects", [])
        if item.get("identity")
    }
    removed_ids = {
        str(item.get("identity", "")) for item in body.get("removed_projects", [])
        if item.get("identity")
    }
    requested_ids -= removed_ids
    with SCAN_LOCK:
        scan = SCAN_RESULT
    discovered_projects = {
        str(project.get("identity", "")): str(project.get("label", ""))
        for project in (scan.project_dicts if scan else [])
    }
    invalid = sorted(requested_ids - discovered_projects.keys())
    if invalid:
        return JSONResponse(
            {"error": "One or more selected projects are no longer available"},
            status_code=400,
        )
    try:
        watcher = watcher_status()
        existing = load_watcher_config() if watcher.get("config") else None
        projects = [
            AllowedProject(
                identity=identity,
                label=discovered_projects[identity],
            )
            for identity in sorted(requested_ids)
        ]
        if existing:
            projects.extend(
                project
                for project in existing.projects
                if project.identity not in discovered_projects
                and project.identity not in removed_ids
            )
        all_projects = bool(body.get(
            "all_projects",
            existing.all_projects if existing else False,
        ))
        # The override already covers every project, so the boxes it checks are
        # not a choice: freeze the saved selection instead of overwriting it with
        # them. Unchecking a box turns the override off (see the UI), which
        # arrives here as all_projects=false and saves the boxes as they stand.
        if all_projects and existing:
            projects = list(existing.projects)
        config = WatcherConfig(
            auto_uploader_version=(
                existing.auto_uploader_version
                if existing
                else WatcherConfig.auto_uploader_version
            ),
            contributor=_safe_name(body.get("contributor_name", "anonymous")),
            aws_profile=existing.aws_profile if existing else selected_profile(),
            all_projects=all_projects,
            projects=projects,
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


def main(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
    strict_port: bool = False,
) -> int:
    base = port if port is not None else int(os.environ.get("PORT", "8899"))
    port = base if strict_port else _find_free_port(base, host=host)
    if port is None:
        print(f"No free port found in {base}-{base + 19}; is something stuck?")
        return 1
    if strict_port and _find_free_port(base, host=host, tries=1) is None:
        print(f"Port {base} is already in use.")
        return 1
    if port != base:
        print(f"Port {base} is in use — using {port} instead.")
    if open_browser:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://localhost:{port}")
        ).start()
        print(f"Opening browser at http://localhost:{port}")
    else:
        print(f"Serving UI at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
