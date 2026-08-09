from agent_transcript_collector import app, cli, tui


def test_ui_subcommand_dispatches_to_web_ui(monkeypatch):
    called = []
    monkeypatch.setattr(
        app,
        "main",
        lambda headless=False, contributor_name="anonymous": (
            called.append((headless, contributor_name)) or 0
        ),
    )

    assert cli.main(["ui", "--all", "--name", "alice"]) == 0
    assert called == [(True, "alice")]


def test_tui_subcommand_dispatches_to_terminal_browser(monkeypatch):
    called = []
    monkeypatch.setattr(
        tui, "main", lambda prefix="mts-trans/": called.append(prefix) or 0
    )

    assert cli.main(["tui", "--prefix", "mts-trans/alice/"]) == 0
    assert called == ["mts-trans/alice/"]
