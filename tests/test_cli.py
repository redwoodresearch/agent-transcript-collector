import pytest

from agent_transcript_collector import app, cli, tui


def test_ui_subcommand_dispatches_to_web_ui(monkeypatch):
    called = []
    monkeypatch.setattr(app, "main", lambda: called.append(True) or 0)

    assert cli.main(["ui"]) == 0
    assert called == [True]


def test_ui_rejects_bulk_upload_flags():
    with pytest.raises(SystemExit):
        cli.main(["ui", "--all", "--name", "alice"])


def test_tui_subcommand_dispatches_to_terminal_browser(monkeypatch):
    called = []
    monkeypatch.setattr(
        tui, "main", lambda prefix="mts-trans/": called.append(prefix) or 0
    )

    assert cli.main(["tui", "--prefix", "mts-trans/alice/"]) == 0
    assert called == ["mts-trans/alice/"]


def test_watcher_subcommand_dispatches_to_watcher(monkeypatch):
    from agent_transcript_collector import watcher

    called = []
    monkeypatch.setattr(watcher, "main", lambda argv: called.append(argv) or 0)

    assert cli.main(["watcher", "uninstall", "--purge"]) == 0
    assert called == [["uninstall", "--purge"]]
