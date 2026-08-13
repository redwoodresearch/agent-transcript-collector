"""Base abstractions shared by all transcript sources.

A "source" is one agent harness (Claude Code, Codex, Pi, ...). Each source
knows where that harness stores transcripts on disk, how to discover them,
and how to parse a transcript into messages for preview. Everything
downstream (redaction, zipping, upload, the UI) is source-agnostic and works in
terms of the normalized types defined here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..paths import project_identity_cache_path
from ..attachments import AttachmentSet

_CODEX_WORKTREE_RE = re.compile(
    r"(?P<root>.*?/\.?codex/worktrees/[^/]+/(?P<name>[^/]+))(?:/|$)"
)
_IDENTITY_CACHE_LOCK = threading.Lock()
_identity_cache: dict[str, dict[str, str]] | None = None


def _normalize_identity_cache(data: dict) -> dict[str, dict[str, str]]:
    normalized = {}
    for key, value in data.items():
        if isinstance(value, dict) and value.get("identity") and value.get("name"):
            normalized[str(key)] = {
                "identity": str(value["identity"]),
                "name": str(value["name"]),
            }
        elif isinstance(value, str):
            normalized[str(key)] = {
                "identity": value,
                "name": Path(value).name or "_root",
            }
    return normalized


def _load_identity_cache() -> dict[str, dict[str, str]]:
    global _identity_cache
    with _IDENTITY_CACHE_LOCK:
        if _identity_cache is None:
            try:
                data = json.loads(project_identity_cache_path().read_text())
                _identity_cache = _normalize_identity_cache(data)
            except (OSError, ValueError, json.JSONDecodeError):
                _identity_cache = {}
        return dict(_identity_cache)


def _remember_project_identity(worktree: str, identity: str, name: str) -> None:
    global _identity_cache
    with _IDENTITY_CACHE_LOCK:
        if _identity_cache is None:
            try:
                data = json.loads(project_identity_cache_path().read_text())
                _identity_cache = _normalize_identity_cache(data)
            except (OSError, ValueError, json.JSONDecodeError):
                _identity_cache = {}
        value = {"identity": identity, "name": name}
        if _identity_cache.get(worktree) == value:
            return
        _identity_cache[worktree] = value
        target = project_identity_cache_path()
        temporary: str | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_identity_cache, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except OSError:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, skipping blank/garbage lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def truncate(text: str, max_length: int = 200) -> str:
    text = text.strip()
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def project_identity(cwd: str) -> tuple[str, str]:
    """Return a stable project key and a human-readable repository name.

    Agent harnesses frequently run inside temporary worktrees. Treat those
    checkouts as the repository they belong to instead of exposing ephemeral
    paths in the UI. For live paths, the nearest Git root determines the
    project; stale paths fall back to their final directory name.
    """
    normalized = cwd.replace("\\", "/").rstrip("/")
    worktree_match = _CODEX_WORKTREE_RE.match(normalized)
    worktree_root = worktree_match.group("root") if worktree_match else None
    directory = canonical_project_directory(normalized)
    if directory is not None:
        name = Path(directory).name or "_root"
        identity = directory
        if worktree_root:
            _remember_project_identity(worktree_root, identity, name)
    else:
        name = (
            worktree_match.group("name")
            if worktree_match
            else Path(normalized).name or "_root"
        )
        cached = _load_identity_cache().get(worktree_root) if worktree_root else None
        # A previously observed live worktree keeps its primary-repository
        # identity after deletion. An unknown stale worktree falls back to its
        # full root, avoiding collisions between same-named repositories.
        if cached:
            identity = cached["identity"]
            name = cached["name"]
        else:
            identity = f"stale:{worktree_root or normalized}"

    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"_project-{name}-{digest}", name


def canonical_project_directory(cwd: str) -> str | None:
    """Return the primary project directory when it can be identified safely."""
    normalized = cwd.replace("\\", "/").rstrip("/")
    if not normalized:
        return None

    for marker in ("/.claude/worktrees/", "/.agents/worktrees/"):
        if marker in normalized:
            return normalized.split(marker, 1)[0]

    path = Path(normalized).expanduser()
    if not path.is_absolute():
        return None

    for candidate in (path, *path.parents):
        dotgit = candidate / ".git"
        if dotgit.is_dir():
            return str(candidate)
        if not dotgit.is_file():
            continue
        try:
            first_line = dotgit.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return str(candidate)
        if not first_line.startswith("gitdir:"):
            return str(candidate)
        git_dir = Path(first_line.split(":", 1)[1].strip()).expanduser()
        if not git_dir.is_absolute():
            git_dir = (candidate / git_dir).resolve()
        common_file = git_dir / "commondir"
        try:
            common_dir = (git_dir / common_file.read_text(encoding="utf-8").strip()).resolve()
        except OSError:
            return str(candidate)
        if common_dir.name == ".git":
            return str(common_dir.parent)
        return str(candidate)

    if re.search(r"/\.?codex/worktrees/[^/]+/", normalized):
        return None
    return normalized


def decode_existing_project_path(encoded: str) -> str | None:
    """Recover a dash-encoded absolute path only when it exists on disk."""
    parts = [part for part in encoded.replace("\\", "-").strip("-").split("-") if part]
    current = Path("/")
    while parts:
        match = None
        for count in range(len(parts), 0, -1):
            candidate = current / "-".join(parts[:count])
            if candidate.exists():
                match = (candidate, count)
                break
        if match is None:
            return None
        current, consumed = match
        parts = parts[consumed:]
    return str(current)


@dataclass
class Session:
    source: str          # source id, e.g. "claude_code"
    id: str              # session id, unique within (source, project)
    project_id: str      # canonical identity assigned by scan.py
    project_label: str   # human-readable project label
    project_directory: str | None
    path: Path           # absolute path to the transcript file on disk
    size_bytes: int
    first_message: str
    message_count: int
    modified: datetime | None = None
    is_subagent: bool = False      # spawned task subagent, not a top-level session
    parent: str | None = None      # parent session id, when is_subagent
    child_ids: tuple[str, ...] = ()  # discovered direct children, for ATIF links

    @property
    def size_human(self) -> str:
        return human_size(self.size_bytes)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "first_message": self.first_message,
            "message_count": self.message_count,
            "size_bytes": self.size_bytes,
            "size_human": self.size_human,
            "modified": self.modified,
            "is_subagent": self.is_subagent,
            "parent": self.parent,
        }


@runtime_checkable
class Source(Protocol):
    id: str                # stable slug, used in S3 prefixes and URLs
    label: str             # display name
    source_format: str     # format tag recorded in the manifest

    def discover(self) -> list[Session]:
        """Return transcripts found on disk (empty if not installed)."""
        ...

    def parse_messages(self, raw: str) -> list[dict]:
        """Parse raw (possibly redacted) transcript text into [{role, text}]."""
        ...


@runtime_checkable
class AttachmentSource(Protocol):
    """A source whose transcripts point at agent-visible files stored elsewhere."""

    def attachments(self, session: Session, raw_text: str) -> AttachmentSet:
        """Resolve the attachments an unredacted transcript points at."""
        ...


def session_attachments(source, session: Session, raw_text: str) -> AttachmentSet:
    """Return a source's attachments, or none when it has no external files."""
    if isinstance(source, AttachmentSource):
        return source.attachments(session, raw_text)
    return AttachmentSet()
