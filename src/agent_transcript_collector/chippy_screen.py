"""LLM-assisted PII screening for the chippy importer.

The base secret redactor and the email pass are rule-based and fail safe (they
redact by default). Two categories can't be handled by a blanket rule without
over-redacting, because whether a token is personal is a judgment call:

- **GitHub handles** — ``github.com/torvalds`` is a person; ``github.com/anthropics``
  is an org and useful to keep. Redacting *every* handle would strip org/repo
  context that makes a transcript readable.
- **Personal names as bare tokens** — a first name in a ``/home/<user>/`` path or
  a git author line is personal; a service account (``ubuntu``, ``agent``) is not.

This module gathers those candidates from the transcripts actually being imported
and asks Claude to classify each as personal (redact) or not (keep), so the
importer scrubs real identifiers without a hand-maintained allow/deny list.

Optional dependency: install the ``llm`` extra (``anthropic``) and provide
credentials via the standard chain (``ANTHROPIC_API_KEY`` or an ``ant auth login``
profile). Enabled with ``chippy-importer --llm-screen``.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, NamedTuple

from . import redactor

# github.com/<handle> — same context the redactor scrubs, so we only surface
# handles that would actually be redacted if screened in.
_HANDLE_RE = re.compile(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))", re.IGNORECASE)
# Home-path usernames: the concrete "name" candidates. Default logins are dropped.
_HOMEUSER_RE = re.compile(r"/(?:home|Users)/([A-Za-z0-9][A-Za-z0-9._-]*)")

# Per-classification request cap, so a run with thousands of distinct handles is
# chunked into several bounded calls rather than one oversized prompt.
_BATCH = 100
_MAX_CANDIDATES = 2000  # hard ceiling; anything beyond is reported and skipped


class ScreenResult(NamedTuple):
    names: set[str]
    handles: set[str]


def extract_candidates(texts: Iterable[str]) -> dict[str, set[str]]:
    """Collect distinct candidate handles + home-path usernames across ``texts``.

    Default/system logins (ubuntu, ec2-user, ...) are dropped here so the LLM
    only spends judgment on genuinely ambiguous tokens.
    """
    defaults = redactor.default_usernames()
    handles: set[str] = set()
    names: set[str] = set()
    for text in texts:
        for m in _HANDLE_RE.finditer(text):
            handles.add(m.group(1))
        for m in _HOMEUSER_RE.finditer(text):
            u = m.group(1)
            if u.lower() not in defaults and len(u) >= redactor.MIN_USERNAME_LEN:
                names.add(u)
    return {"handles": handles, "names": names}


_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "kind": {"type": "string", "enum": ["handle", "name"]},
                    "personal": {"type": "boolean"},
                },
                "required": ["value", "kind", "personal"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You screen tokens pulled from AI coding-agent transcripts before they are "
    "shared for research. For each token decide whether it identifies a specific "
    "real person (personal=true) or is non-personal (personal=false).\n"
    "- handle: a GitHub handle. Personal if it is an individual's account; not "
    "personal if it is an organization, bot, or project (e.g. anthropics, "
    "openai, actions-user, dependabot).\n"
    "- name: a username taken from a filesystem path. Personal if it is a real "
    "person's name/login; not personal if it is a generic service or role "
    "account (agent, runner, deploy, build, svc, admin).\n"
    "When unsure, mark personal=true — over-redacting a non-personal token is "
    "cheap; leaking a real identity is not."
)


def _classify_batch(client, model: str, batch: list[tuple[str, str]]) -> ScreenResult:
    listing = "\n".join(f"- {kind}: {value}" for value, kind in batch)
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": f"Classify each token:\n{listing}"}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    names, handles = set(), set()
    for c in data.get("classifications", []):
        if not c.get("personal"):
            continue
        if c.get("kind") == "handle":
            handles.add(c["value"])
        elif c.get("kind") == "name":
            names.add(c["value"])
    return ScreenResult(names=names, handles=handles)


def classify(candidates: dict[str, set[str]], model: str) -> ScreenResult:
    """Classify candidate handles + names via Claude. Returns the personal ones."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised via the CLI extra
        raise SystemExit(
            "--llm-screen needs the 'llm' extra: uv pip install "
            "'agent-transcript-collector[llm]' (and ANTHROPIC_API_KEY or an "
            "'ant auth login' profile)."
        ) from exc

    items = ([(v, "handle") for v in sorted(candidates["handles"])]
             + [(v, "name") for v in sorted(candidates["names"])])
    if len(items) > _MAX_CANDIDATES:
        print(f"chippy_screen: {len(items)} candidates exceeds cap {_MAX_CANDIDATES}; "
              f"screening the first {_MAX_CANDIDATES}, leaving the rest unredacted.")
        items = items[:_MAX_CANDIDATES]

    client = anthropic.Anthropic()
    names, handles = set(), set()
    for i in range(0, len(items), _BATCH):
        res = _classify_batch(client, model, items[i:i + _BATCH])
        names |= res.names
        handles |= res.handles
    return ScreenResult(names=names, handles=handles)


def screen_runs(texts: Iterable[str], model: str) -> ScreenResult:
    """Extract candidates from ``texts`` and classify them. Convenience wrapper."""
    candidates = extract_candidates(texts)
    if not candidates["handles"] and not candidates["names"]:
        return ScreenResult(names=set(), handles=set())
    return classify(candidates, model)
