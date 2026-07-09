"""Tests for the chippy importer — redaction policy, run listing, and zip building.

No network: a FakeS3 stands in for both the source (list + get) and dest (head +
put) buckets, mirroring the style of test_download.py.
"""

import io
import json
import re
import zipfile

import pytest

from agent_transcript_collector import chippy_import, redactor
from agent_transcript_collector.chippy_import import RedactionPolicy

MOCK = re.compile(redactor._MOCK_TAG, re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Redactor helpers (added for the importer)
# --------------------------------------------------------------------------- #

class TestRedactorHelpers:
    def test_emails_redacted_except_keep_list(self):
        text = "reach ryan@gmail.com or noreply@anthropic.com for help"
        out, n = redactor.redact_emails(text, keep={"noreply@anthropic.com"})
        assert "ryan@gmail.com" not in out
        assert "noreply@anthropic.com" in out  # kept
        assert n == 1

    def test_home_path_users_redacted_but_defaults_kept(self):
        text = "/home/tyler/work and /home/ubuntu/logs"
        out, n = redactor.redact_home_path_users(text)
        assert out == "/home/[USER]/work and /home/ubuntu/logs"
        assert n == 1

    def test_named_users_whole_token_case_insensitive(self):
        text = "Eric and eric_the_cat and americas"  # only the bare token matches
        out, n = redactor.redact_named_users(text, ["eric"])
        assert out == "[USER] and eric_the_cat and americas"
        assert n == 1

    def test_named_users_ignores_short_and_default_names(self):
        _, n = redactor.redact_named_users("bob ubuntu", ["bob", "ubuntu"])
        assert n == 0  # "bob" < MIN_USERNAME_LEN, "ubuntu" is a default login

    def test_github_handles_only_in_url_context(self):
        text = "see github.com/torvalds but torvalds is also a word"
        out, n = redactor.redact_github_handles(text, ["torvalds"])
        assert out == "see github.com/[HANDLE] but torvalds is also a word"
        assert n == 1

    def test_github_handles_empty_is_noop(self):
        out, n = redactor.redact_github_handles("github.com/anyone", [])
        assert n == 0 and out == "github.com/anyone"


# --------------------------------------------------------------------------- #
# RedactionPolicy
# --------------------------------------------------------------------------- #

class TestRedactionPolicy:
    def test_layers_compose_and_order(self):
        policy = RedactionPolicy(
            keep_emails={"noreply@anthropic.com"},
            names=["tyler"],
            handles=["torvalds"],
        )
        raw = (
            "ghp_" + "a" * 36 + "\n"
            "email tyler@corp.com and noreply@anthropic.com\n"
            "github.com/torvalds in /home/tyler/x\n"
        )
        red, counts = policy.apply(raw)
        assert MOCK.search(red)                      # secret mocked
        assert counts["secret"] == 1
        assert "tyler@corp.com" not in red           # personal email gone
        assert "noreply@anthropic.com" in red        # automated kept
        assert "github.com/[HANDLE]" in red
        assert "/home/[USER]/x" in red
        # tyler@corp.com became [EMAIL], not [USER]@corp.com (email ran first)
        assert "[USER]@corp.com" not in red

    def test_extend_merges(self):
        policy = RedactionPolicy(names=["a"], handles=["x"])
        policy.extend(names=["b"], handles=["y"])
        assert policy.names == {"a", "b"}
        assert policy.handles == {"x", "y"}


# --------------------------------------------------------------------------- #
# Run listing / selection / zip building with a fake S3
# --------------------------------------------------------------------------- #

class FakeS3:
    def __init__(self, objects: dict[str, bytes], present: set[str] | None = None):
        self._objects = objects
        self._present = present or set()   # dest keys that already exist
        self.put_keys: dict[str, bytes] = {}

    # source side
    def get_paginator(self, _name):
        objects = self._objects

        class _Pag:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}

        return _Pag()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._objects[Key])}

    # dest side
    def head_object(self, Bucket, Key):
        if Key in self._present:
            return {}
        raise RuntimeError("404")

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.put_keys[Key] = Body


SOURCE = {
    "runs/run-a/transcripts/s1.jsonl": b'{"text":"hi from github.com/torvalds"}\n',
    "runs/run-a/transcripts/subagents/agent-1.jsonl": b'{"text":"/home/tyler/x"}\n',
    "runs/run-b/transcripts/s1.jsonl": b'{"text":"ghp_' + b"a" * 36 + b'"}\n',
    "runs/run-b/other/ignore.txt": b"not a transcript",
}


def test_list_source_runs_groups_and_filters():
    s3 = FakeS3(SOURCE)
    runs = chippy_import.list_source_runs(s3, "src", "runs/")
    assert set(runs) == {"run-a", "run-b"}
    assert len(runs["run-a"]) == 2          # both .jsonl, subagent included
    assert all(k.endswith(".jsonl") for k in runs["run-a"])  # .txt excluded


def test_select_runs_smallest_first_and_explicit():
    runs = {"big": [1, 2, 3], "small": [1]}
    assert [r for r, _ in chippy_import.select_runs(runs, None, 0)] == ["small", "big"]
    assert [r for r, _ in chippy_import.select_runs(runs, ["big"], 0)] == ["big"]
    assert [r for r, _ in chippy_import.select_runs(runs, None, 1)] == ["small"]


def test_build_run_zip_redacts_and_manifests(tmp_path):
    s3 = FakeS3(SOURCE)
    policy = RedactionPolicy(names=["tyler"], handles=["torvalds"])
    data, manifest = chippy_import.build_run_zip(
        s3, "src", "run-a", sorted(SOURCE)[:2], policy, tmp_path
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "transcripts/s1.jsonl" in names
        body = zf.read("transcripts/s1.jsonl").decode()
        assert "github.com/[HANDLE]" in body
    assert manifest["run_id"] == "run-a"
    assert manifest["transcript_count"] == 2
    assert manifest["subagent_count"] == 1        # the subagents/ path
    # mirror was populated (raw cache)
    assert (tmp_path / "runs/run-a/transcripts/s1.jsonl").exists()


def test_read_transcript_uses_mirror_second_time(tmp_path):
    calls = {"n": 0}

    class CountingS3(FakeS3):
        def get_object(self, Bucket, Key):
            calls["n"] += 1
            return super().get_object(Bucket, Key)

    s3 = CountingS3(SOURCE)
    key = "runs/run-b/transcripts/s1.jsonl"
    first = chippy_import.read_transcript(s3, "src", key, tmp_path)
    second = chippy_import.read_transcript(s3, "src", key, tmp_path)
    assert first == second
    assert calls["n"] == 1  # second read served from mirror


def test_dest_exists_head_check():
    s3 = FakeS3(SOURCE, present={"chippy/run-a/transcripts.zip"})
    assert chippy_import.dest_exists(s3, "dst", "run-a") is True
    assert chippy_import.dest_exists(s3, "dst", "run-b") is False
