"""One-way migrations for persisted watcher configuration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .paths import watcher_config_path
from .sources import SOURCES, projects_from_groups
from .watcher import (
    AllowedProject,
    ProjectMember,
    SOURCE_ENV_VARS,
    WatcherConfig,
    save_config,
)


CURRENT_SCHEMA_VERSION = 2


def _v1_to_v2(data: dict) -> dict:
    configured = {
        (str(item.get("source", "")), str(item.get("group", "")))
        for item in data.get("groups", [])
        if item.get("source") and item.get("group")
    }
    source_env = {
        str(key): str(value)
        for key, value in data.get("source_env", {}).items()
        if key in SOURCE_ENV_VARS
    }
    previous_env = {name: os.environ.get(name) for name in source_env}
    os.environ.update(source_env)
    try:
        discovered = [
            (source, group)
            for source in SOURCES
            for group in source.discover()
        ]
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    projects = []
    matched = set()
    for project in projects_from_groups(discovered):
        members = {
            (harness["source"], group)
            for harness in project["harnesses"]
            for group in harness["groups"]
        }
        selected = configured & members
        if not selected:
            continue
        matched.update(selected)
        projects.append(AllowedProject(
            identity=project["identity"],
            label=project["label"],
            members=tuple(
                ProjectMember(source, group)
                for source, group in sorted(members)
            ),
        ))

    missing = configured - matched
    if missing:
        formatted = ", ".join(f"{source}:{group}" for source, group in sorted(missing))
        raise ValueError(
            "Could not migrate watcher configuration because these selected "
            f"transcript groups are unavailable: {formatted}"
        )

    config = WatcherConfig(
        auto_uploader_version=int(data.get("auto_uploader_version", 0)),
        contributor=str(data.get("contributor", "anonymous")),
        aws_profile=str(data.get("aws_profile", "rw-eng")),
        projects=projects,
        source_env=source_env,
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
    while version != CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f"unsupported watcher configuration version: {version}")
        data = migration(data)
        version = data.get("schema_version")
    config = WatcherConfig.from_dict(data)
    save_config(config, target)
    return config
