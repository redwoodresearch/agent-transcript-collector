import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from agent_transcript_collector.uploader import upload_transcripts


class FakePaginator:
    def __init__(self, s3):
        self.s3 = s3

    def paginate(self, *, Bucket, Prefix):
        return [
            {
                "Contents": [
                    {"Key": key, "Size": len(body)}
                    for key, body in self.s3.objects.items()
                    if key.startswith(Prefix)
                ]
            }
        ]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def put_object(self, *, Key, Body, **kwargs):
        self.objects[Key] = Body


def session(path: Path, *, session_id="session-1", parent=None):
    return SimpleNamespace(
        id=session_id,
        group_key="_project-example-123456789abc",
        group_label="example",
        path=path,
        size_bytes=path.stat().st_size,
        is_subagent=parent is not None,
        parent=parent,
    )


def source():
    return SimpleNamespace(id="test", source_format="test-jsonl")


def test_uploads_one_zip_per_transcript_and_skips_an_existing_version(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","content":"hello"}\n')
    s3 = FakeS3()

    first, first_errors = upload_transcripts(s3, source(), [session(path)], "alice")
    second, second_errors = upload_transcripts(s3, source(), [session(path)], "alice")

    assert len(first) == 1
    assert first_errors == second_errors == []
    assert second == []
    assert list(s3.objects) == [first[0]["s3_key"]]
    assert first[0]["s3_key"].startswith("mts-trans/alice/example--")
    assert "/test/session-1/" in first[0]["s3_key"]

    with zipfile.ZipFile(io.BytesIO(next(iter(s3.objects.values())))) as archive:
        assert set(archive.namelist()) == {"transcript.jsonl", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["project"]["name"] == "example"
    assert manifest["session"]["id"] == "session-1"


def test_redaction_policy_creates_a_distinct_transcript_version(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","content":"/Users/alice/example"}\n')
    s3 = FakeS3()

    redacted, _ = upload_transcripts(
        s3, source(), [session(path)], "alice", redact_id=True
    )
    unredacted, _ = upload_transcripts(
        s3, source(), [session(path)], "alice", redact_id=False
    )

    assert redacted[0]["s3_key"] != unredacted[0]["s3_key"]
    assert len(s3.objects) == 2


def test_resuming_a_session_adds_a_content_version(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","content":"first"}\n')
    s3 = FakeS3()

    first, _ = upload_transcripts(s3, source(), [session(path)], "alice")
    path.write_text(
        '{"role":"user","content":"first"}\n{"role":"assistant","content":"second"}\n'
    )
    resumed, _ = upload_transcripts(s3, source(), [session(path)], "alice")

    assert len(s3.objects) == 2
    assert (
        first[0]["s3_key"].rsplit("/", 1)[0] == resumed[0]["s3_key"].rsplit("/", 1)[0]
    )


def test_subagent_is_nested_under_its_parent_session(tmp_path):
    path = tmp_path / "child.jsonl"
    path.write_text('{"role":"user","content":"delegated"}\n')
    s3 = FakeS3()

    uploaded, errors = upload_transcripts(
        s3,
        source(),
        [session(path, session_id="child-id", parent="parent-id")],
        "alice",
    )

    assert errors == []
    assert "/parent-id/subagents/child-id/" in uploaded[0]["s3_key"]
    assert "/subagents/subagents/" not in uploaded[0]["s3_key"]
