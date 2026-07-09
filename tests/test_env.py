import os

from agent_transcript_collector.env import load_local_env


def test_load_local_env_loads_working_directory_file_without_overriding(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("CTC_TEST_ENV=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CTC_TEST_ENV", raising=False)

    load_local_env()

    assert os.environ["CTC_TEST_ENV"] == "from-dotenv"
    monkeypatch.setenv("CTC_TEST_ENV", "from-shell")
    load_local_env()
    assert os.environ["CTC_TEST_ENV"] == "from-shell"
