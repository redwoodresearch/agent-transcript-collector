"""S3 key layout and identities for collected transcripts."""

import hashlib
import re

from .sources.base import Session

STORAGE_PREFIX = "mts-trans"


def _safe_segment(value: str) -> str:
    value = value.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-") or "unknown"
    if cleaned != value:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}--{suffix}"
    return cleaned


def _project_segment(session: Session) -> str:
    value = session.project_label
    if not value:
        return "%00"
    return value.replace("%", "%25").replace("/", "%2F").replace("\\", "%5C")


def transcript_filename(transcript_id: str) -> str:
    """Return the portable sibling filename for one transcript archive."""
    return f"{_safe_segment(transcript_id)}.zip"


def transcript_prefix(contributor: str, session: Session) -> str:
    """Return the flat contributor/project prefix for one transcript."""
    return (
        f"{STORAGE_PREFIX}/{_safe_segment(contributor)}/"
        f"{_project_segment(session)}/"
    )


def transcript_key(contributor: str, session: Session, transcript_id: str) -> str:
    prefix = transcript_prefix(contributor, session)
    return f"{prefix}{transcript_filename(transcript_id)}"
