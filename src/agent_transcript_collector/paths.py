"""Per-user paths shared by watcher configuration and upload locking."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "agent-transcript-collector"


def config_dir() -> Path:
    override = os.environ.get("CTC_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def state_dir() -> Path:
    override = os.environ.get("CTC_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME


def log_path() -> Path:
    if sys.platform == "darwin" and not os.environ.get("CTC_STATE_DIR"):
        return Path.home() / "Library" / "Logs" / APP_NAME / "watcher.log"
    return state_dir() / "watcher.log"


def ui_log_path() -> Path:
    if sys.platform == "darwin" and not os.environ.get("CTC_STATE_DIR"):
        return Path.home() / "Library" / "Logs" / APP_NAME / "ui.log"
    return state_dir() / "ui.log"


def watcher_config_path() -> Path:
    return Path(os.environ.get("CTC_WATCHER_CONFIG", config_dir() / "watcher.json"))


def watcher_state_path() -> Path:
    return Path(os.environ.get("CTC_WATCHER_STATE", state_dir() / "watcher-state.json"))


def upload_lock_path() -> Path:
    return Path(os.environ.get("CTC_UPLOAD_LOCK", state_dir() / "upload.lock"))


def project_identity_cache_path() -> Path:
    return state_dir() / "project-identities.json"


def pipeline_cache_path() -> Path:
    return state_dir() / "pipeline-cache.json"


def prepared_artifacts_dir() -> Path:
    return state_dir() / "prepared"
