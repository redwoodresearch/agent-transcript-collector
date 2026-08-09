import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_transcript_collector import app
from agent_transcript_collector import watcher
from agent_transcript_collector.redactor import redact_path_token
from agent_transcript_collector.sources.base import project_identity
from agent_transcript_collector.uploader import UploadBusy, UploadLock


def test_same_named_repositories_have_distinct_keys(tmp_path):
    first = tmp_path / "one" / "project"
    second = tmp_path / "two" / "project"
    (first / ".git").mkdir(parents=True)
    (second / ".git").mkdir(parents=True)

    first_key, first_name = project_identity(str(first))
    second_key, second_name = project_identity(str(second))

    assert first_name == second_name == "project"
    assert first_key != second_key


def test_linked_worktree_uses_main_repository_key(tmp_path):
    repository = tmp_path / "project"
    git_dir = repository / ".git"
    worktree = tmp_path / "worktree"
    worktree_git_dir = git_dir / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    worktree_git_dir.mkdir(parents=True)
    (worktree_git_dir / "commondir").write_text("../..\n")
    (worktree / ".git").write_text(f"gitdir: {worktree_git_dir}\n")

    assert project_identity(str(worktree))[0] == project_identity(str(repository))[0]


def test_project_name_is_not_treated_as_an_encoded_home_path():
    token = "_project-home-assistant-123456789abc"
    assert redact_path_token(token, usernames=()) == (token, 0)


def test_upload_lock_rejects_a_second_process_lock(tmp_path):
    path = tmp_path / "upload.lock"
    with UploadLock(path):
        with pytest.raises(UploadBusy):
            with UploadLock(path):
                pass


def test_watcher_captures_cursor_user_data_dir(monkeypatch):
    monkeypatch.setenv("CURSOR_USER_DATA_DIR", "/custom/cursor")
    assert watcher.capture_source_env()["CURSOR_USER_DATA_DIR"] == "/custom/cursor"


def test_failed_launchd_install_removes_service_file(tmp_path, monkeypatch):
    service_path = tmp_path / "watcher.plist"
    monkeypatch.setattr(watcher, "launchd_path", lambda: service_path)
    monkeypatch.setattr(watcher, "log_path", lambda: tmp_path / "watcher.log")
    monkeypatch.setattr(watcher, "_find_uv", lambda: "/usr/bin/uv")

    def run_command(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stderr=b"activation failed")

    with pytest.raises(RuntimeError, match="activation failed"):
        watcher.install(
            watcher.WatcherConfig(),
            config_path=tmp_path / "config.json",
            platform="darwin",
            run_command=run_command,
        )

    assert not service_path.exists()


def test_watcher_update_does_not_reinstall_an_active_service(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SOURCES", [])
    monkeypatch.setattr(app, "watcher_status", lambda: {"installed": True})
    monkeypatch.setattr(
        app,
        "install_watcher",
        lambda config: pytest.fail("an active watcher should not be reinstalled"),
    )
    monkeypatch.setattr(
        app,
        "save_watcher_config",
        lambda config: tmp_path / "config.json",
    )

    response = TestClient(app.app).put(
        "/api/watcher",
        json={
            "groups": [],
            "contributor_name": "alice",
            "redact_identity": False,
            "enabled": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["installed"] is True
