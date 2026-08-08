from pathlib import Path
from types import SimpleNamespace

from agent_transcript_collector.uploader import upload_units


class FakePaginator:
    def __init__(self, s3):
        self.s3 = s3

    def paginate(self, *, Bucket, Prefix):
        contents = [
            {"Key": key}
            for key in self.s3.objects
            if key.startswith(Prefix)
        ]
        return [{"Contents": contents}]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def put_object(self, *, Key, Body, **kwargs):
        self.objects[Key] = Body


def session(path: Path):
    return SimpleNamespace(
        id="session-1",
        group_key="_project-example",
        group_label="example",
        path=path,
        size_bytes=path.stat().st_size,
        is_subagent=False,
        parent=None,
    )


def source():
    return SimpleNamespace(id="test", source_format="test-jsonl")


def zip_keys(s3):
    return {key for key in s3.objects if key.endswith(".zip")}


def test_upload_units_skips_an_existing_archive(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","content":"hello"}\n')
    s3 = FakeS3()

    first, first_errors = upload_units(s3, source(), [session(path)], "alice")
    second, second_errors = upload_units(s3, source(), [session(path)], "alice")

    assert len(first) == 1
    assert first_errors == []
    assert second == []
    assert second_errors == []
    assert len(zip_keys(s3)) == 1


def test_redaction_policy_has_its_own_archive_version(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","content":"/Users/alice/example"}\n')
    s3 = FakeS3()

    redacted, _ = upload_units(
        s3, source(), [session(path)], "alice", redact_id=True
    )
    unredacted, _ = upload_units(
        s3, source(), [session(path)], "alice", redact_id=False
    )

    assert len(redacted) == 1
    assert len(unredacted) == 1
    assert redacted[0]["s3_key"] != unredacted[0]["s3_key"]
    assert len(zip_keys(s3)) == 2
