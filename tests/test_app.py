"""Tests for app-level helpers."""

import socket

from agent_transcript_collector.app import _find_free_port


def test_find_free_port_skips_occupied():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen()
    occupied = s.getsockname()[1]
    try:
        port = _find_free_port(occupied, tries=50)
        assert port is not None and port != occupied
        # the returned port is actually bindable
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", port))
        probe.close()
    finally:
        s.close()


def test_find_free_port_returns_start_when_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()  # now free
    assert _find_free_port(free, tries=5) == free


from pathlib import Path
from agent_transcript_collector import app as appmod
from agent_transcript_collector import uploader
from agent_transcript_collector.sources.base import Session


def _sess(sid, group="-home-u-proj", size=10, parent=None, path=Path("/nonexistent")):
    return Session(source="claude_code", id=sid, group_key=group, group_label="/home/u/proj",
                   path=path, size_bytes=size, first_message="", message_count=0,
                   is_subagent=bool(parent), parent=parent)


def test_plan_units_small_group_single_unit():
    units = list(uploader._plan_units([_sess("a"), _sess("b")]))
    assert len(units) == 1 and len(units[0][2]) == 2


def test_plan_units_splits_oversized_group(monkeypatch):
    units = list(uploader._plan_units(
        [_sess(f"s{i}", size=60) for i in range(3)], unit_bytes=100))
    assert len(units) == 3 and all(len(m) == 1 for _, _, m in units)


def test_plan_units_never_splits_one_session(monkeypatch):
    units = list(uploader._plan_units(
        [_sess("big", size=1000)], unit_bytes=10))
    assert len(units) == 1 and len(units[0][2]) == 1


def test_plan_units_deterministic_regardless_of_order():
    s = [_sess("b"), _sess("a"), _sess("c")]
    a = [[x.id for x in m[2]] for m in uploader._plan_units(s)]
    b = [[x.id for x in m[2]] for m in uploader._plan_units(list(reversed(s)))]
    assert a == b


class _FakeS3:
    def __init__(self): self.objs = {}
    def put_object(self, Bucket, Key, Body, ContentType):
        self.objs[Key] = Body


class _FakePaginator:
    def __init__(self, objs): self.objs = objs
    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": key} for key in self.objs if key.startswith(Prefix)]}]


class _ListableFakeS3(_FakeS3):
    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return _FakePaginator(self.objs)


def test_receipts_are_stable_and_listable():
    sess = _sess("x", parent="parent")
    s3 = _ListableFakeS3()
    key = uploader._receipt_key("claude_code", "tester", sess, "content")
    s3.objs[key] = b"archive-key"

    assert key == uploader._receipt_key(
        "claude_code", "tester", sess, "content")
    identity = uploader._receipt_token(sess.group_key, sess.parent, sess.id)
    assert uploader.list_receipt_versions(
        s3, "claude_code", "tester") == {identity: {"content"}}
    assert uploader.list_receipt_versions(
        s3, "claude_code", "someone-else") == {}


def test_upload_units_deterministic_overwrite(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","message":{"content":"hi"}}\n')

    class Src:
        id = "claude_code"
        source_format = "claude-jsonl"

    sess = _sess("x", size=f.stat().st_size, path=f)
    s3 = _FakeS3()
    ticks = []
    up1, err1 = appmod._upload_units(s3, Src(), [sess], "tester", on_unit=lambda n: ticks.append(n))
    assert len(up1) == 1 and err1 == []
    key1 = up1[0]["s3_key"]
    up2, _ = appmod._upload_units(s3, Src(), [sess], "tester")
    assert len(up2) == 1 and up2[0]["s3_key"] == key1   # deterministic key -> overwrite in place
    assert len(s3.objs) == 2                            # one archive + one stable receipt
    receipt = uploader._receipt_key(
        "claude_code", "tester", sess, uploader.content_fingerprint(sess))
    assert receipt in s3.objs
    assert sum(ticks) == 1


def test_upload_units_collects_per_unit_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(uploader, "UPLOAD_CONCURRENCY", 1)   # deterministic for the test
    sessions = []
    for i in range(3):
        f = tmp_path / f"s{i}.jsonl"
        f.write_text('{"type":"user","message":{"content":"hi"}}\n')
        sessions.append(_sess(f"s{i}", size=f.stat().st_size, path=f))

    class Src:
        id = "claude_code"
        source_format = "claude-jsonl"

    class FlakyS3:
        def __init__(self): self.objs = {}; self.calls = 0
        def put_object(self, Bucket, Key, Body, ContentType):
            self.calls += 1
            if self.calls == 1:
                raise Exception("boom")
            self.objs[Key] = Body

    ticks = []
    up, errs = uploader.upload_units(
        FlakyS3(), Src(), sessions, "t",
        on_unit=lambda n: ticks.append(n), unit_bytes=10)
    assert len(errs) == 1 and len(up) == 2     # one unit failed, others still uploaded
    assert sum(ticks) == 3                      # progress ticks for every unit, success or fail


def test_uploaded_endpoint_matches_receipts(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from agent_transcript_collector.sources.base import Group

    transcript = tmp_path / "existing.jsonl"
    transcript.write_text('{"message":"hello"}\n')
    sess = _sess("existing", parent="parent", path=transcript)
    s3 = _ListableFakeS3()
    receipt = uploader._receipt_key(
        "claude_code", "tester", sess, uploader.content_fingerprint(sess))
    s3.objs[receipt] = b"archive-key"
    monkeypatch.setattr(appmod, "_make_s3_client", lambda: s3)
    class Source:
        def discover(self):
            return [Group(sess.group_key, sess.group_label, [sess])]
    monkeypatch.setattr(
        appmod, "get_source",
        lambda source_id: Source() if source_id == "claude_code" else None)
    descriptor = {
        "source": "claude_code",
        "group": sess.group_key,
        "session": sess.id,
        "parent": sess.parent,
    }

    response = TestClient(appmod.app).post(
        "/api/uploaded",
        json={"contributor_name": "tester", "sessions": [
            descriptor,
            {**descriptor, "session": "new"},
        ]},
    )

    assert response.status_code == 200
    assert response.json() == {"uploaded": [descriptor]}


def test_watcher_endpoint_validates_discovered_groups(monkeypatch):
    from fastapi.testclient import TestClient
    from agent_transcript_collector.sources.base import Group

    class Source:
        id = "claude_code"
        def discover(self):
            return [Group("accepted", "/projects/accepted", [])]

    captured = {}
    monkeypatch.setattr(appmod, "SOURCES", [Source()])
    monkeypatch.setattr(
        appmod,
        "install_watcher",
        lambda config: captured.setdefault("config", config) or {"installed": True},
    )
    client = TestClient(appmod.app)

    invalid = client.put("/api/watcher", json={
        "groups": [{"source": "claude_code", "group": "other"}],
    })
    valid = client.put("/api/watcher", json={
        "contributor_name": "alice",
        "groups": [{"source": "claude_code", "group": "accepted"}],
    })

    assert invalid.status_code == 400
    assert valid.status_code == 200
    assert captured["config"].contributor == "alice"
    assert captured["config"].groups[0].group == "accepted"


def test_upload_job_endpoints():
    from fastapi.testclient import TestClient
    import time as _t
    c = TestClient(appmod.app)
    assert c.post("/api/upload", json={"selected": []}).status_code == 400
    r = c.post("/api/upload", json={"selected": [{"source": "nope", "group": "g", "session": "s"}],
                                    "contributor_name": "t"})
    assert r.status_code == 202
    jid = r.json()["job_id"]
    st = {}
    for _ in range(100):
        st = c.get("/api/upload/" + jid).json()
        if st["status"] in ("completed", "partial", "failed"):
            break
        _t.sleep(0.01)
    assert st["status"] == "completed"      # bogus source resolves to nothing
    assert c.get("/api/upload/nope").status_code == 404
