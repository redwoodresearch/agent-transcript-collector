"""Files holding agent-visible content that a transcript only points at.

Harnesses spill oversized tool output into separate files and leave a pointer
behind ("Output too large. Full output saved to: <path>"), so a transcript on
its own records that the agent saw something without recording what. Each
source knows its own pointer syntax and folders; this module holds the rules
they share: a pointer is followed only when it lands on a real file inside a
directory that harness owns, and one session's side files are capped in total
size.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BUDGET_BYTES = 100 * 1024 * 1024


def budget_bytes() -> int:
    """Total side-file bytes allowed per session before the rest are dropped."""
    override = os.environ.get("CTC_SIDECAR_MAX_BYTES", "").strip()
    if not override:
        return DEFAULT_BUDGET_BYTES
    try:
        return max(0, int(override))
    except ValueError:
        return DEFAULT_BUDGET_BYTES


@dataclass(frozen=True)
class Sidecar:
    path: Path        # absolute path on this machine
    reference: str    # the path as the transcript writes it
    arcname: str      # path inside the uploaded ZIP
    kind: str         # archive folder, e.g. "tool-results"
    size_bytes: int


@dataclass(frozen=True)
class SidecarSet:
    files: tuple[Sidecar, ...] = ()
    missing: tuple[str, ...] = ()    # pointers whose target is already gone
    skipped: tuple[str, ...] = ()    # dropped once the size budget ran out

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


EMPTY = SidecarSet()


class SidecarBuilder:
    """Gather one session's side files, then freeze them into a `SidecarSet`."""

    def __init__(self, roots: Sequence[Path], budget: int | None = None):
        self._roots = []
        for root in roots:
            try:
                self._roots.append((root, root.resolve(strict=True)))
            except (OSError, RuntimeError):
                continue
        self._budget = budget_bytes() if budget is None else budget
        self._found: dict[Path, tuple[str, str]] = {}
        self._missing: set[str] = set()

    def add(self, pointer: str, kind: str) -> None:
        """Record the file a transcript pointer names, if it is still there."""
        candidate = Path(pointer)
        if not candidate.is_absolute():
            return
        claimed = next(
            (real_root for root, real_root in self._roots if candidate.is_relative_to(root)),
            None,
        )
        if claimed is None:
            return
        try:
            real = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            self._missing.add(pointer)
            return
        # Transcript text is untrusted, and a pointer that leaves its own
        # harness folder through a symlink is not this session's side file:
        # Claude Code, for one, points a finished task at the subagent
        # transcript the collector already uploads on its own.
        if real.is_file() and real.is_relative_to(claimed):
            self._found.setdefault(real, (kind, pointer))

    def add_directory(self, directory: Path, kind: str) -> None:
        """Record every file in a folder the session owns outright."""
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            self.add(str(entry), kind)

    def build(self) -> SidecarSet:
        files: list[Sidecar] = []
        skipped: list[str] = []
        used: set[str] = set()
        spent = 0
        ordered = sorted(self._found.items(), key=lambda item: (item[1][0], str(item[0])))
        for path, (kind, reference) in ordered:
            try:
                size = path.stat().st_size
            except OSError:
                self._missing.add(reference)
                continue
            if spent + size > self._budget:
                skipped.append(reference)
                continue
            files.append(
                Sidecar(path, reference, self._arcname(path, kind, used), kind, size)
            )
            spent += size
        return SidecarSet(tuple(files), tuple(sorted(self._missing)), tuple(skipped))

    @staticmethod
    def _arcname(path: Path, kind: str, used: set[str]) -> str:
        name = path.name
        arcname = f"{kind}/{name}" if name else ""
        if not name or arcname in used:
            # A resumed session keeps pointing at the folder it inherited, so
            # one archive can hold same-named files from different sessions.
            digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]
            stem, suffix = (Path(name).stem, Path(name).suffix) if name else ("file", "")
            arcname = f"{kind}/{stem}-{digest}{suffix}"
        used.add(arcname)
        return arcname
