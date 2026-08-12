"""One-way migrations for persisted watcher configuration."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .paths import watcher_config_path
from .watcher import (
    AllowedProject,
    ProjectMember,
    SOURCE_ENV_VARS,
    WatcherConfig,
    load_config,
    save_config,
)


CURRENT_SCHEMA_VERSION = 2


def _v1_to_v2(data: dict) -> dict:
    projects = [
        AllowedProject(
            identity=f"v1:{index}",
            label=str(item.get("label", "")),
            members=(
                ProjectMember(
                    source=str(item["source"]),
                    group=str(item["group"]),
                ),
            ),
        )
        for index, item in enumerate(data.get("groups", []))
        if item.get("source") and item.get("group")
    ]

    config = WatcherConfig(
        auto_uploader_version=int(data.get("auto_uploader_version", 0)),
        contributor=str(data.get("contributor", "anonymous")),
        aws_profile=str(data.get("aws_profile", "rw-eng")),
        projects=projects,
        source_env={
            str(key): str(value)
            for key, value in data.get("source_env", {}).items()
            if key in SOURCE_ENV_VARS
        },
        package_spec=str(data.get("package_spec", WatcherConfig.package_spec)),
        uv_path=str(data.get("uv_path", "")),
    )
    return asdict(config)


MIGRATIONS = {
    1: _v1_to_v2,
}


def migrate_config(path: Path | None = None) -> WatcherConfig:
    """Upgrade the watcher config to the current schema, then load it."""
    target = path or watcher_config_path()
    data = json.loads(target.read_text())
    version = data.get("schema_version")
    if version == CURRENT_SCHEMA_VERSION:
        return load_config(target)
    while version != CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f"unsupported watcher configuration version: {version}")
        data = migration(data)
        version = data.get("schema_version")
    config = WatcherConfig.from_dict(data)
    save_config(config, target)
    return config
