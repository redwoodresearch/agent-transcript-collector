"""Registry of source-specific transcript adapters."""

from __future__ import annotations

from .base import Source
from .claude_code import ClaudeCodeSource
from .codex import CodexSource
from .cursor import CursorSource
from .pi import PiSource

SOURCES: list[Source] = [ClaudeCodeSource(), CodexSource(), CursorSource(), PiSource()]

_BY_ID = {s.id: s for s in SOURCES}


def get_source(source_id: str) -> Source | None:
    return _BY_ID.get(source_id)
