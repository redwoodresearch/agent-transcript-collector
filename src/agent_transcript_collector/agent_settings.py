"""Record what the agent was permitted to do, from its settings files.

A transcript shows which tool calls were refused, but not what was allowed
before the session started: a pre-approved `Bash(gcloud …)` rule leaves no
trace, so a reader cannot tell an authorised command from one the user was
asked about. The rules live in settings files on disk, which this reads.

Only permission-shaped settings are taken. Environment variables are recorded
by name and never by value: a settings file is a plausible place for an API key
to sit, and nothing here is worth that risk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Settings are layered: user, then project, then the project's local overrides,
# which is where per-project grants accumulate.
_PROJECT_FILES = (".claude/settings.json", ".claude/settings.local.json")
_PERMISSION_KEYS = ("allow", "deny", "ask", "defaultMode", "additionalDirectories")


def user_settings_path() -> Path:
    root = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return Path(root) / "settings.json"


def _summarize(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    permissions = data.get("permissions")
    record: dict = {"path": str(path)}
    if isinstance(permissions, dict):
        record["permissions"] = {
            key: permissions[key] for key in _PERMISSION_KEYS if key in permissions
        }
    if isinstance(data.get("model"), str):
        record["model"] = data["model"]
    environment = data.get("env")
    if isinstance(environment, dict) and environment:
        # Names only: the values are the user's business and may be secrets.
        record["env_names"] = sorted(environment)
    return record if len(record) > 1 else None


def describe(source_id: str, project_directory: str | None) -> dict:
    """Summarize the permission rules in force for one session's project."""
    if source_id != "claude_code":
        return {"status": "unsupported"}
    files = [user_settings_path()]
    if project_directory:
        files.extend(Path(project_directory) / name for name in _PROJECT_FILES)

    layers = [record for record in (_summarize(path) for path in files) if record]
    if not layers:
        return {"status": "none_found"}
    allowed = sum(
        len(layer.get("permissions", {}).get("allow", []) or []) for layer in layers
    )
    return {"status": "captured", "allow_rule_count": allowed, "layers": layers}
