"""Run Claude Code while recording the system prompt it actually sent.

Claude Code does not write its system prompt to the transcript; it is visible
only on the API request. OpenTelemetry can dump raw request bodies to disk, but
each body contains the whole conversation so far, so a long session writes
hundreds of megabytes. This wrapper turns that firehose into a few kilobytes:
it watches the dump directory, keeps the largest `system` block it sees, and
deletes each body as soon as it has been read.

The prompt is written to the collector's state directory keyed by session id,
where the next upload picks it up.

Usage:
    python tools/capture_system_prompt.py -- [claude args...]
    python tools/capture_system_prompt.py -- -p "explain this repo"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from agent_transcript_collector.system_prompt import capture_path, digest_of

POLL_SECONDS = 0.5


def _system_text(body: dict) -> str | None:
    system = body.get("system")
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        parts = [
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        joined = "\n\n".join(part for part in parts if part)
        return joined or None
    return None


def _drain(directory: Path, best: dict) -> None:
    """Read new request bodies, keep the largest prompt, delete the bodies.

    The largest is the one that matters: Claude Code also makes small side
    calls (naming the session, for instance) whose prompts are not the agent's.
    """
    for path in sorted(directory.glob("*.json")):
        try:
            if path.name.endswith(".request.json"):
                text = _system_text(json.loads(path.read_text()))
                if text and len(text) > len(best.get("text") or ""):
                    best["text"] = text
        except (OSError, ValueError):
            continue
        finally:
            # Bodies hold the entire conversation; none of it is wanted here.
            try:
                path.unlink()
            except OSError:
                pass


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] == "--":
        args = args[1:]

    session_id = str(uuid.uuid4())
    if "--session-id" in args:
        session_id = args[args.index("--session-id") + 1]
    else:
        args = ["--session-id", session_id, *args]

    dump_dir = Path(tempfile.mkdtemp(prefix="ctc-otel-"))
    environment = {
        **os.environ,
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_LOG_RAW_API_BODIES": f"file:{dump_dir}",
    }

    best: dict = {}
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            _drain(dump_dir, best)
            time.sleep(POLL_SECONDS)
        _drain(dump_dir, best)

    watcher = threading.Thread(target=watch, daemon=True, name="otel-drain")
    watcher.start()
    try:
        completed = subprocess.run(["claude", *args], env=environment, check=False)
    finally:
        stop.set()
        watcher.join(timeout=10)
        shutil.rmtree(dump_dir, ignore_errors=True)

    text = best.get("text")
    if not text:
        print("no system prompt captured (was a request made?)", file=sys.stderr)
        return completed.returncode

    target = capture_path("claude_code", session_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps({
        "session_id": session_id,
        "sha256": digest_of(text),
        "chars": len(text),
        "text": text,
    }, indent=2))
    target.chmod(0o600)
    print(f"captured system prompt: {len(text)} chars -> {target}", file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
