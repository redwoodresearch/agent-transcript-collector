import io
import zipfile

from agent_transcript_collector import download
from agent_transcript_collector.catalog import Unit


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    def get_object(self, Bucket: str, Key: str):
        return {"Body": io.BytesIO(self._objects[Key])}


class BoomS3(FakeS3):
    def get_object(self, Bucket: str, Key: str):
        raise RuntimeError("network down")


def test_extract_unit_reassembles_tree_and_writes_marker(tmp_path):
    body = make_zip(
        {
            "manifest.json": b'{"source":"claude_code"}',
            "myproj/sess1.jsonl": b'{"x":1}\n',
            "myproj/subagents/agent-1.jsonl": b'{"y":2}\n',
        }
    )
    unit = Unit("claude_code/alice/g123/part-000-abc.zip", len(body), "claude_code", "alice")

    download._extract_unit(unit, body, tmp_path)

    base = tmp_path / "claude_code" / "alice"
    assert (base / "myproj" / "sess1.jsonl").read_bytes() == b'{"x":1}\n'
    assert (base / "myproj" / "subagents" / "agent-1.jsonl").exists()
    marker = base / "_manifests" / "part-000-abc.json"
    assert marker.read_bytes() == b'{"source":"claude_code"}'
    assert download._is_done(unit, tmp_path, extract=True)


def test_contributorless_key_extracts_under_source(tmp_path):
    body = make_zip({"manifest.json": b"{}", "transcripts/a.jsonl": b"hi"})
    unit = Unit("swe_zero/part-0001-x.zip", len(body), "swe_zero", "")
    download._extract_unit(unit, body, tmp_path)
    assert (tmp_path / "swe_zero" / "transcripts" / "a.jsonl").read_bytes() == b"hi"


def test_download_units_extract_then_idempotent(tmp_path):
    key = "codex/bob/g1/part-000-x.zip"
    body = make_zip({"manifest.json": b"{}", "g/s.jsonl": b"hello"})
    s3 = FakeS3({key: body})
    units = [Unit(key, len(body), "codex", "bob")]

    ok, skipped, errors = download.download_units(
        s3, "bucket", units, tmp_path, extract=True, concurrency=1
    )
    assert (ok, skipped, errors) == (1, 0, [])
    assert (tmp_path / "codex" / "bob" / "g" / "s.jsonl").read_bytes() == b"hello"

    ok2, skipped2, errors2 = download.download_units(
        s3, "bucket", units, tmp_path, extract=True, concurrency=1
    )
    assert (ok2, skipped2, errors2) == (0, 1, [])


def test_download_units_no_extract_keeps_zip(tmp_path):
    key = "swe_zero/part-0001-x.zip"
    body = make_zip({"manifest.json": b"{}"})
    s3 = FakeS3({key: body})
    units = [Unit(key, len(body), "swe_zero", "")]

    ok, skipped, _ = download.download_units(
        s3, "bucket", units, tmp_path, extract=False, concurrency=1
    )
    assert ok == 1
    assert (tmp_path / key).read_bytes() == body

    _, skipped2, _ = download.download_units(
        s3, "bucket", units, tmp_path, extract=False, concurrency=1
    )
    assert skipped2 == 1  # size matches -> skipped


def test_download_units_collects_errors(tmp_path):
    key = "codex/bob/g1/part-000-x.zip"
    units = [Unit(key, 10, "codex", "bob")]
    ok, skipped, errors = download.download_units(
        BoomS3({}), "bucket", units, tmp_path, extract=True, concurrency=1
    )
    assert ok == 0 and skipped == 0
    assert len(errors) == 1 and "network down" in errors[0][1]


def test_s3_prefix_hint():
    parse = download._build_parser().parse_args
    assert download._s3_prefix_hint(parse(["--prefix", "claude_code/x/"])) == "claude_code/x/"
    assert download._s3_prefix_hint(parse(["--source", "codex"])) == "codex/"
    # multiple sources -> no single narrowing prefix
    assert download._s3_prefix_hint(parse(["--source", "codex", "--source", "pi"])) is None
    assert download._s3_prefix_hint(parse([])) is None
