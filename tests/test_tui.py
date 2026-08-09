from agent_transcript_collector.catalog import Unit
from agent_transcript_collector.tui import build_catalog_tree


def test_catalog_tree_preserves_the_full_s3_hierarchy():
    units = [
        Unit(
            key="codex/alice/group-a/part-001.zip",
            size=10,
            source="codex",
            contributor="alice",
        ),
        Unit(
            key="codex/bob/group-b/part-002.zip",
            size=20,
            source="codex",
            contributor="bob",
        ),
    ]

    root = build_catalog_tree(units)

    assert set(root.children) == {"codex"}
    assert set(root.children["codex"].children) == {"alice", "bob"}
    assert root.children["codex"].units() == units
    assert root.children["codex"].children["alice"].units() == [units[0]]
