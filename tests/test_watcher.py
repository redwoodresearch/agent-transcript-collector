"""Tests for persisted consent and native watcher behavior."""

import json
import plistlib
from pathlib import Path

import pytest

from agent_transcript_collector import uploader, watcher
from agent_transcript_collector.sources.base import Group, Session


def _session(path: Path, sid: str = "session", group: str = "group") -> Session:
    return Session(
        source="test",
        id=sid,
        group_key=group,
        group_label=f"/projects/{group}",
        path=path,
        size_bytes=path.stat().st_size,
        first_message="hello",
        message_count=1,
    )


class FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        return [{
            "Contents": [
                {"Key": key} for key in self.objects if key.startswith(Prefix)
            ]
        }]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return FakePaginator(self.objects)


class FakeSource:
    id = "test"
    label = "Test"
    source_format = "test-jsonl"

    def __init__(self, groups):
        self.groups = groups

    def discover(self):
        return self.groups


def _config(group="group"):
    return watcher.WatcherConfig(
        contributor="alice",
        aws_profile="rw-eng",
        groups=[watcher.AllowedGroup("test", group, f"/projects/{group}")],
        uv_path="/opt/uv",
    )


def test_config_round_trip_is_private(tmp_path):
    path = tmp_path / "private" / "watcher.json"
    watcher.save_config(_config(), path)

    loaded = watcher.load_config(path)
    assert loaded.contributor == "alice"
    assert loaded.groups == [watcher.AllowedGroup("test", "group", "/projects/group")]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_config_rejects_unknown_version(tmp_path):
    path = tmp_path / "watcher.json"
    path.write_text('{"schema_version": 2}')
    with pytest.raises(ValueError, match="unsupported"):
        watcher.load_config(path)


def test_discover_allowed_uses_exact_source_and_group(monkeypatch, tmp_path):
    accepted_file = tmp_path / "accepted.jsonl"
    other_file = tmp_path / "other.jsonl"
    accepted_file.write_text("accepted")
    other_file.write_text("other")
    source = FakeSource([
        Group("group", "/projects/group", [_session(accepted_file)]),
        Group("group-child", "/projects/group/child", [
            _session(other_file, "other", "group-child")
        ]),
    ])
    monkeypatch.setattr(watcher, "SOURCES", [source])

    found = watcher.discover_allowed(_config())

    assert found == [(source, source.groups[0].sessions)]


def test_changed_content_creates_new_version(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("first")
    session = _session(transcript)
    source = FakeSource([Group("group", "/projects/group", [session])])
    monkeypatch.setattr(watcher, "SOURCES", [source])
    s3 = FakeS3()
    state = tmp_path / "state.json"
    lock = tmp_path / "upload.lock"

    first = watcher.run_once(_config(), s3=s3, state_path=state, lock_path=lock)
    unchanged = watcher.run_once(_config(), s3=s3, state_path=state, lock_path=lock)
    transcript.write_text("second")
    session.size_bytes = transcript.stat().st_size
    changed = watcher.run_once(_config(), s3=s3, state_path=state, lock_path=lock)

    assert first["sessions_uploaded"] == 1
    assert unchanged["sessions_uploaded"] == 0
    assert changed["sessions_uploaded"] == 1
    archives = [key for key in s3.objects if key.endswith(".zip")]
    assert len(archives) == 2
    assert json.loads(state.read_text())["status"] == "completed"


def test_receipt_listing_failure_does_not_upload(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("content")
    source = FakeSource([Group("group", "/projects/group", [_session(transcript)])])
    monkeypatch.setattr(watcher, "SOURCES", [source])

    class BrokenS3(FakeS3):
        def get_paginator(self, operation):
            raise PermissionError("no list permission")

    result = watcher.run_once(
        _config(),
        s3=BrokenS3(),
        state_path=tmp_path / "state.json",
        lock_path=tmp_path / "lock",
    )

    assert result["status"] == "failed"
    assert result["sessions_uploaded"] == 0


def test_upload_lock_prevents_overlap(tmp_path):
    path = tmp_path / "upload.lock"
    with uploader.UploadLock(path):
        with pytest.raises(uploader.UploadBusy):
            with uploader.UploadLock(path):
                pass


def test_native_service_rendering(tmp_path):
    config = _config()
    path = tmp_path / "watcher.json"

    plist = plistlib.loads(watcher.render_launchd(config, path))
    service, timer = watcher.render_systemd(config, path)

    assert plist["StartInterval"] == 3600
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"][-2:] == ["--config", str(path)]
    assert b"Type=oneshot" in service
    assert b"OnCalendar=hourly" in timer
    assert b"Persistent=true" in timer
    assert b"RandomizedDelaySec=5m" in timer


def test_linux_install_is_idempotent(monkeypatch, tmp_path):
    config_path = tmp_path / "config" / "watcher.json"
    service_path = tmp_path / "systemd" / "collector.service"
    timer_path = tmp_path / "systemd" / "collector.timer"
    calls = []

    class Completed:
        returncode = 0
        stderr = b""

    def run(command, **kwargs):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(
        watcher, "systemd_paths", lambda: (service_path, timer_path)
    )
    watcher.install(
        _config(),
        config_path=config_path,
        platform="linux",
        run_command=run,
    )
    first = (service_path.read_bytes(), timer_path.read_bytes())
    watcher.install(
        _config(),
        config_path=config_path,
        platform="linux",
        run_command=run,
    )

    assert first == (service_path.read_bytes(), timer_path.read_bytes())
    assert calls[-2:] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", timer_path.name],
    ]
