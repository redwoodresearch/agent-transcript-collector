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
from .pipeline import artifacts_for, mark_uploaded, refresh as refresh_pipeline
from .paths import watcher_config_path
from .s3client import make_s3_client as _make_s3_client
from .s3client import selected_profile
from .sources import SOURCES, get_source, projects_from_groups
from .sources.base import human_size, session_sidecars
from .uploader import (
    UploadBusy,
    UploadLock,
    artifact_is_available,
    upload_artifacts,
)
from .watcher import (
    AllowedProject,
    ProjectMember,
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
        if (
            SCAN_CACHE["projects"] is not None
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
        return JSONResponse({"error": "Session not found; try Refresh"}, status_code=404)

    try:
        raw = Path(sess.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return JSONResponse(
            {"error": "Session file is no longer available; try Refresh"},
            status_code=404,
        )
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


# --- one background scan → redact changed → check changed pipeline ---
PIPELINE_RUNS: dict[str, dict] = {}
PIPELINE_LOCK = threading.Lock()
_active_pipeline = {"id": None}


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

            result = refresh_pipeline(
                selections, contributor, on_progress=progress
            )
    except Exception as exc:
        result = {
            "status": "failed", "items": [],
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            "total": sum(len(sessions) for _, sessions in selections),
            "changed": 0, "checked": 0, "cached": 0,
        }
    finally:
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
            "status": "running", "stage": "redacting", "done": 0,
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


def _upload_worker(artifacts, contributor, events):
    """Upload exactly the artifacts produced by the persistent pipeline."""
    uploads = []
    errors = []
    status = "failed"
    try:
        with UploadLock():
            if not all(artifact_is_available(item) for item in artifacts):
                raise RuntimeError("A prepared upload is no longer available. Refresh and try again.")
            events.put({
                "type": "progress", "status": "running", "stage": "uploading",
                "stage_done": 0, "stage_total": len(artifacts),
                "stage_item_done": 0, "stage_item_total": len(artifacts),
            })
            completed = 0

            def advance(count):
                nonlocal completed
                completed += count
                events.put({"type": "advance", "count": count})
                events.put({
                    "type": "progress", "stage": "uploading",
                    "stage_done": completed, "stage_total": len(artifacts),
                    "stage_item_done": completed,
                    "stage_item_total": len(artifacts),
                })

            uploads, errors = upload_artifacts(
                _make_s3_client(), artifacts, on_progress=advance
            )
            successful = {
                (item.get("source"), item.get("group"), item.get("parent") or "",
                 item.get("session"))
                for item in uploads
            }
            uploaded_artifacts = [
                item for item in artifacts
                if (item.get("source"), item.get("group"),
                    item.get("parent") or "", item.get("session")) in successful
            ]
            if uploaded_artifacts:
                mark_uploaded(uploaded_artifacts, contributor)
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
    artifacts, stale = artifacts_for(selections, contributor)
    if stale:
        return JSONResponse(
            {"error": "Prepared uploads are stale. Refresh before uploading."},
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
            "stage": "uploading",
            "stage_done": 0,
            "stage_total": len(artifacts),
            "total": len(artifacts),
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
            args=(artifacts, contributor, events),
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
        projects = list(SCAN_CACHE["projects"] or [])
    visible_projects = [
        (
            project,
            {
                (harness["source"], group)
                for harness in project["harnesses"]
                for group in harness["groups"]
            },
        )
        for project in projects
    ]
    selected = []
    for saved in config.get("projects", []):
        members = {
            (item.get("source", ""), item.get("group", ""))
            for item in saved.get("members", [])
        }
        current = next((
            project
            for project, groups in visible_projects
            if saved.get("identity") == project.get("identity")
            or bool(members & groups)
        ), None)
        selected.append({
            "identity": current.get("identity", ""),
            "label": current.get("label", ""),
        } if current else saved)
    config["projects"] = list({
        item.get("identity", ""): item for item in selected
    }.values())
    return result


def _put_watcher(body: dict):
    requested = {
        (str(item.get("identity", "")), str(item.get("label", "")))
        for item in body.get("projects", [])
    }
    removed = {
        (str(item.get("identity", "")), str(item.get("label", "")))
        for item in body.get("removed_projects", [])
    }
    requested -= removed
    with SCAN_LOCK:
        discovered_projects = {
            (str(project.get("identity", "")), str(project.get("label", ""))): {
                (harness["source"], group)
                for harness in project["harnesses"]
                for group in harness["groups"]
            }
            for project in (SCAN_CACHE["projects"] or [])
        }
    invalid = sorted(requested - discovered_projects.keys())
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
                label=label,
                members=tuple(sorted(
                    (
                        ProjectMember(source, group)
                        for source, group in discovered_projects[(identity, label)]
                    ),
                    key=lambda item: (item.source, item.group),
                )),
            )
            for identity, label in sorted(requested)
        ]
        if existing:
            visible_groups = set().union(
                *discovered_projects.values(),
            ) if discovered_projects else set()
            projects.extend(
                project
                for project in existing.projects
                if (
                    (project.identity, project.label) not in discovered_projects
                    and not (
                        {(member.source, member.group) for member in project.members}
                        & visible_groups
                    )
                )
                and (project.identity, project.label) not in removed
            )
        config = WatcherConfig(
            auto_uploader_version=(
                existing.auto_uploader_version
                if existing
                else WatcherConfig.auto_uploader_version
            ),
            contributor=_safe_name(body.get("contributor_name", "anonymous")),
            aws_profile=existing.aws_profile if existing else selected_profile(),
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
    config_path = watcher_config_path()
    if config_path.exists():
        from .migrate import migrate_config

        migrate_config(config_path)
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
