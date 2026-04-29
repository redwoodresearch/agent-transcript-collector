"""Scan ~/.claude/projects/ to discover transcripts."""

import json
import platform
from datetime import datetime
from pathlib import Path


def get_claude_dir() -> Path:
    if platform.system() == "Windows":
        return Path.home() / ".claude"
    return Path.home() / ".claude"


def get_projects_dir() -> Path:
    return get_claude_dir() / "projects"


def decode_project_name(encoded: str) -> str:
    """Decode folder name back to a path.

    e.g. '-Users-mylesheller-Git-foo' -> '/Users/mylesheller/Git/foo'
    """
    if not encoded:
        return encoded
    parts = encoded.split("-")
    if parts[0] == "":
        parts = parts[1:]
    return "/" + "/".join(parts)


def get_first_user_message(filepath: Path, max_length: int = 200) -> str:
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        texts = [
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        text = " ".join(texts)
                    else:
                        text = str(content)
                    text = text.strip()
                    if len(text) > max_length:
                        text = text[:max_length] + "..."
                    return text
        return "(empty session)"
    except Exception:
        return "(unreadable)"


def get_session_timestamp(filepath: Path) -> datetime | None:
    try:
        stat = filepath.stat()
        return datetime.fromtimestamp(stat.st_mtime)
    except Exception:
        return None


def count_messages(filepath: Path) -> int:
    count = 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") in ("user", "assistant"):
                    count += 1
    except Exception:
        pass
    return count


def scan_projects() -> list[dict]:
    """Return a list of projects with their sessions."""
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return []

    results = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        sessions = []
        for session_file in sorted(project_dir.glob("*.jsonl")):
            size = session_file.stat().st_size
            sessions.append({
                "id": session_file.stem,
                "filename": session_file.name,
                "path": str(session_file),
                "size_bytes": size,
                "size_human": _human_size(size),
                "modified": get_session_timestamp(session_file),
                "first_message": get_first_user_message(session_file),
                "message_count": count_messages(session_file),
            })

        if not sessions:
            continue

        total_size = sum(s["size_bytes"] for s in sessions)
        results.append({
            "encoded_name": project_dir.name,
            "decoded_path": decode_project_name(project_dir.name),
            "session_count": len(sessions),
            "total_size_bytes": total_size,
            "total_size_human": _human_size(total_size),
            "sessions": sessions,
        })

    return results


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"
