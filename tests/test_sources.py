"""Tests for the multi-source transcript adapters."""

import json

import pytest

from claude_transcript_collector.sources import (
    detect_all,
    find_session,
    get_source,
)
from claude_transcript_collector.sources.claude_code import ClaudeCodeSource
from claude_transcript_collector.sources.codex import CodexSource
from claude_transcript_collector.sources.pi import PiSource


def _write_jsonl(path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in objs), encoding="utf-8")


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Point every source at isolated temp dirs so real ~/.* is never scanned."""
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    pi_agent = tmp_path / "pi" / "agent"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_agent))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    return {
        "claude_projects": claude / "projects",
        "codex_sessions": codex / "sessions",
        "pi_agent": pi_agent,
        "pi_sessions": pi_agent / "sessions",
    }


def _seed_claude(iso):
    _write_jsonl(iso["claude_projects"] / "-home-u-proj" / "sess-uuid.jsonl", [
        {"type": "user", "message": {"content": "Hello claude"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}},
    ])


def _seed_codex(iso):
    uuid = "11111111-2222-3333-4444-555555555555"
    _write_jsonl(
        iso["codex_sessions"] / "2026" / "06" / "24" / f"rollout-2026-06-24T10-00-00-{uuid}.jsonl",
        [
            {"type": "session_meta", "payload": {"cwd": "/home/u/proj", "id": uuid, "source": "cli"}},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hello codex"}]},
            {"type": "response_item", "payload": {
                "role": "assistant", "content": [{"type": "output_text", "text": "hey"}]}},
        ],
    )
    return uuid


def _seed_codex_source(iso, source, uuid, ts="11-00-00"):
    """A rollout with an arbitrary session_meta `source` value (real Codex schema)."""
    _write_jsonl(
        iso["codex_sessions"] / "2026" / "06" / "24" / f"rollout-2026-06-24T{ts}-{uuid}.jsonl",
        [
            {"type": "session_meta", "payload": {"cwd": "/home/u/proj", "id": uuid, "source": source}},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "response_item", "payload": {
                "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}},
        ],
    )
    return uuid


def _seed_pi(iso):
    _write_jsonl(iso["pi_sessions"] / "--home-u-proj--" / "2026-06-24T10-00-00-000Z_sess-123.jsonl", [
        {"type": "session", "version": 3, "id": "sess-123", "cwd": "/home/u/proj"},
        {"type": "message", "id": "a1", "parentId": None,
         "message": {"role": "user", "content": "hello pi"}},
        {"type": "message", "id": "a2", "parentId": "a1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hi"}, {"type": "toolCall", "name": "Bash"}]}},
    ])


# --- Claude Code ---

def test_claude_discover_and_group(iso):
    _seed_claude(iso)
    groups = ClaudeCodeSource().discover()
    assert len(groups) == 1
    g = groups[0]
    assert g.label == "/home/u/proj"
    assert g.session_count == 1
    s = g.sessions[0]
    assert s.id == "sess-uuid"
    assert s.first_message == "Hello claude"
    assert s.message_count == 2


def test_claude_parse_messages(iso):
    _seed_claude(iso)
    raw = (iso["claude_projects"] / "-home-u-proj" / "sess-uuid.jsonl").read_text()
    msgs = ClaudeCodeSource().parse_messages(raw)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["text"] == "Hi"


# --- Codex ---

def test_codex_discover_tolerant(iso):
    uuid = _seed_codex(iso)
    groups = CodexSource().discover()
    assert len(groups) == 1
    g = groups[0]
    assert g.label == "/home/u/proj"
    s = g.sessions[0]
    assert s.id == uuid
    assert s.first_message == "hello codex"
    assert s.message_count == 2  # user + assistant


def test_codex_excludes_scaffolding(iso):
    # review / compact / memory_consolidation / internal are dropped; cli kept.
    _seed_codex(iso)  # source: "cli" -> kept
    _seed_codex_source(iso, {"subagent": "review"}, "99999999-0000-0000-0000-000000000001", "11-01-00")
    _seed_codex_source(iso, {"subagent": "compact"}, "99999999-0000-0000-0000-000000000002", "11-02-00")
    _seed_codex_source(iso, {"subagent": "memory_consolidation"}, "99999999-0000-0000-0000-000000000003", "11-03-00")
    _seed_codex_source(iso, {"internal": "memory_consolidation"}, "99999999-0000-0000-0000-000000000004", "11-04-00")
    sessions = [s for g in CodexSource().discover() for s in g.sessions]
    assert len(sessions) == 1
    assert sessions[0].first_message == "hello codex"


def test_codex_only_scaffolding_yields_nothing(iso):
    _seed_codex_source(iso, {"subagent": "review"}, "99999999-0000-0000-0000-000000000005", "11-05-00")
    assert CodexSource().discover() == []


# --- Pi ---

def test_pi_discover_header_and_blocks(iso):
    _seed_pi(iso)
    groups = PiSource().discover()
    assert len(groups) == 1
    g = groups[0]
    assert g.label == "/home/u/proj"
    s = g.sessions[0]
    assert s.id == "sess-123"
    assert s.first_message == "hello pi"
    assert s.message_count == 2  # toolResult/bashExecution roles excluded; this has user+assistant


def test_pi_flat_fallback(iso):
    # Older buggy versions wrote sessions directly into the agent dir.
    _write_jsonl(iso["pi_agent"] / "stray.jsonl", [
        {"type": "session", "version": 3, "id": "flat-1", "cwd": "/home/u/other"},
        {"type": "message", "id": "x", "message": {"role": "user", "content": "stray hi"}},
    ])
    groups = PiSource().discover()
    sessions = [s for g in groups for s in g.sessions]
    assert any(s.id == "flat-1" for s in sessions)


def test_pi_ignores_non_pi_jsonl(iso):
    # A jsonl whose first line isn't a session header must be skipped.
    _write_jsonl(iso["pi_agent"] / "notpi.jsonl", [{"type": "message", "message": {"role": "user", "content": "x"}}])
    assert PiSource().discover() == []


# --- Registry ---

def test_detect_all_only_present_sources(iso):
    _seed_claude(iso)
    _seed_pi(iso)
    detected = detect_all()
    ids = {d["id"] for d in detected}
    assert ids == {"claude_code", "pi"}  # codex absent -> omitted


def test_detect_all_empty(iso):
    assert detect_all() == []


def test_find_session_resolves_path(iso):
    _seed_claude(iso)
    sess = find_session("claude_code", "-home-u-proj", "sess-uuid")
    assert sess is not None
    assert sess.path.exists()
    assert find_session("claude_code", "-home-u-proj", "missing") is None
    assert find_session("nope", "x", "y") is None


def test_source_metadata():
    assert get_source("codex").source_format == "codex-rollout-jsonl"
    assert get_source("pi").source_format == "pi-session-jsonl-v3"
    assert get_source("claude_code").label == "Claude Code"


def _seed_claude_subagent(iso):
    base = iso["claude_projects"] / "-home-u-proj" / "sess-uuid" / "subagents"
    _write_jsonl(base / "agent-x1.jsonl", [
        {"type": "user", "message": {"content": "do a subtask"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ])


def test_claude_discovers_subagents_marked(iso):
    _seed_claude(iso)
    _seed_claude_subagent(iso)
    sessions = {s.id: s for g in ClaudeCodeSource().discover() for s in g.sessions}
    assert sessions["sess-uuid"].is_subagent is False
    assert "agent-x1" in sessions
    assert sessions["agent-x1"].is_subagent is True
    assert sessions["agent-x1"].parent == "sess-uuid"


def test_codex_thread_spawn_kept_and_marked_with_parent(iso):
    # Genuine task subagent: thread_spawn -> kept, marked, parent from parent_thread_id.
    src = {"subagent": {"thread_spawn": {
        "parent_thread_id": "ad7f0408-99b8-4f6e-a46f-bd0eec433370",
        "depth": 1, "agent_nickname": "atlas", "agent_role": "explorer"}}}
    uuid = _seed_codex_source(iso, src, "22222222-3333-4444-5555-666666666666", "12-00-00")
    sessions = {s.id: s for g in CodexSource().discover() for s in g.sessions}
    assert sessions[uuid].is_subagent is True
    assert sessions[uuid].parent == "ad7f0408-99b8-4f6e-a46f-bd0eec433370"


def test_codex_other_subagent_kept_and_marked(iso):
    # catch-all {"subagent": {"other": ...}} -> kept + marked (unknown subagent type).
    uuid = _seed_codex_source(iso, {"subagent": {"other": "atlas"}}, "44444444-0000-0000-0000-000000000001", "12-10-00")
    sessions = {s.id: s for g in CodexSource().discover() for s in g.sessions}
    assert sessions[uuid].is_subagent is True
    assert sessions[uuid].parent is None


def test_codex_cli_and_custom_are_top_level(iso):
    u1 = _seed_codex_source(iso, "cli", "55555555-0000-0000-0000-000000000001", "12-20-00")
    u2 = _seed_codex_source(iso, {"custom": "atlas"}, "55555555-0000-0000-0000-000000000002", "12-21-00")
    sessions = {s.id: s for g in CodexSource().discover() for s in g.sessions}
    assert sessions[u1].is_subagent is False
    assert sessions[u2].is_subagent is False


def test_find_session_disambiguates_subagents_by_parent(iso):
    from claude_transcript_collector.sources import find_session
    proj = iso["claude_projects"] / "-home-u-proj"
    _write_jsonl(proj / "sessA" / "subagents" / "agent-dup.jsonl", [{"type": "user", "message": {"content": "A"}}])
    _write_jsonl(proj / "sessB" / "subagents" / "agent-dup.jsonl", [{"type": "user", "message": {"content": "B"}}])
    a = find_session("claude_code", "-home-u-proj", "agent-dup", parent="sessA")
    b = find_session("claude_code", "-home-u-proj", "agent-dup", parent="sessB")
    assert a is not None and b is not None and a.path != b.path
    assert a.parent == "sessA" and b.parent == "sessB"
