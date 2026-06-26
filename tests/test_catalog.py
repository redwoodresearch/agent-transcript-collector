from agent_transcript_collector.catalog import (
    Unit,
    aggregate_by_contributor,
    aggregate_by_source,
    filter_units,
    list_units,
    parse_key,
)


class FakeListS3:
    """Minimal S3 stand-in supporting get_paginator('list_objects_v2')."""

    def __init__(self, objects: dict[str, int]):
        self._objects = objects  # key -> size

    def get_paginator(self, op: str):
        assert op == "list_objects_v2"
        objects = self._objects

        class _Paginator:
            def paginate(self, **kwargs):
                prefix = kwargs.get("Prefix")
                contents = [
                    {"Key": k, "Size": s}
                    for k, s in objects.items()
                    if not prefix or k.startswith(prefix)
                ]
                yield {"Contents": contents}

        return _Paginator()


def test_parse_key_ignores_non_zip():
    assert parse_key("claude_code/alice/g1/manifest.json", 10) is None
    assert parse_key("claude_code/alice/", 0) is None


def test_parse_key_handles_all_depths():
    u2 = parse_key("swe_zero/part-0001-a.zip", 5)
    assert (u2.source, u2.contributor) == ("swe_zero", "")

    u3 = parse_key("chippy/abc-uuid/transcripts.zip", 5)
    assert (u3.source, u3.contributor) == ("chippy", "abc-uuid")

    u4 = parse_key("claude_code/alice/g1/part-000-x.zip", 5)
    assert (u4.source, u4.contributor) == ("claude_code", "alice")


def test_list_units_filters_non_zip_and_respects_prefix():
    s3 = FakeListS3(
        {
            "claude_code/alice/g1/part-000-x.zip": 100,
            "claude_code/alice/g1/manifest.json": 10,  # not a unit
            "codex/bob/g2/part-000-y.zip": 50,
        }
    )
    all_units = list_units(s3, "bucket")
    assert sorted(u.key for u in all_units) == [
        "claude_code/alice/g1/part-000-x.zip",
        "codex/bob/g2/part-000-y.zip",
    ]
    only_cc = list_units(s3, "bucket", prefix="claude_code/")
    assert [u.source for u in only_cc] == ["claude_code"]


def _sample_units() -> list[Unit]:
    return [
        Unit("claude_code/alice/g1/p.zip", 100, "claude_code", "alice"),
        Unit("claude_code/bob/g2/p.zip", 200, "claude_code", "bob"),
        Unit("codex/alice/g3/p.zip", 50, "codex", "alice"),
        Unit("swe_zero/p.zip", 10, "swe_zero", ""),
    ]


def test_filter_units_by_source_contributor_prefix():
    units = _sample_units()
    assert {u.key for u in filter_units(units, sources=["claude_code"])} == {
        "claude_code/alice/g1/p.zip",
        "claude_code/bob/g2/p.zip",
    }
    assert {u.key for u in filter_units(units, contributors=["alice"])} == {
        "claude_code/alice/g1/p.zip",
        "codex/alice/g3/p.zip",
    }
    assert {u.key for u in filter_units(units, prefix="codex/")} == {
        "codex/alice/g3/p.zip"
    }
    # filters AND together
    assert filter_units(units, sources=["codex"], contributors=["bob"]) == []


def test_aggregates():
    units = _sample_units()
    by_source = aggregate_by_source(units)
    assert by_source["claude_code"].count == 2
    assert by_source["claude_code"].bytes == 300
    assert by_source["swe_zero"].count == 1

    by_contrib = aggregate_by_contributor(units)
    assert by_contrib[("claude_code", "alice")].count == 1
    assert by_contrib[("swe_zero", "")].bytes == 10
