from agent_transcript_collector.tui import S3Object, build_object_tree, list_objects

CHILD_KEY = "mts-trans/alice/project/codex/main/subagents/child/b.zip"


class FakePaginator:
    def paginate(self, **kwargs):
        assert kwargs == {"Bucket": "rr-agent-transcripts", "Prefix": "mts-trans/"}
        return [
            {
                "Contents": [
                    {"Key": "mts-trans/alice/project/codex/main/a.zip", "Size": 10},
                    {"Key": CHILD_KEY, "Size": 20},
                    {"Key": "mts-trans/alice/project/readme.txt", "Size": 30},
                ]
            }
        ]


class FakeS3:
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator()


def test_browser_lists_only_zip_objects():
    assert list_objects(FakeS3(), "mts-trans/") == [
        S3Object("mts-trans/alice/project/codex/main/a.zip", 10),
        S3Object(CHILD_KEY, 20),
    ]


def test_object_tree_preserves_the_full_s3_hierarchy():
    root = build_object_tree(
        [
            S3Object("mts-trans/alice/project/codex/main/a.zip", 10),
            S3Object(CHILD_KEY, 20),
        ]
    )

    main = (
        root.children["mts-trans"]
        .children["alice"]
        .children["project"]
        .children["codex"]
        .children["main"]
    )
    assert main.summary() == (2, 30)
    assert set(main.children) == {"a.zip", "subagents"}
    assert set(main.children["subagents"].children) == {"child"}
