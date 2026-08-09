"""Read-only S3 transcript browser for the ``rr-trans`` command."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Tree
from textual.widgets.tree import TreeNode

from .s3client import S3_BUCKET, make_s3_client
from .sources.base import human_size
from .storage import STORAGE_PREFIX


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int


@dataclass
class ObjectNode:
    """One folder or object in the S3 key hierarchy."""

    name: str
    children: dict[str, ObjectNode] = field(default_factory=dict)
    object_size: int | None = None

    def summary(self) -> tuple[int, int]:
        if self.object_size is not None:
            return 1, self.object_size
        summaries = [child.summary() for child in self.children.values()]
        return sum(count for count, _ in summaries), sum(size for _, size in summaries)


def list_objects(s3, prefix: str) -> list[S3Object]:
    """List transcript ZIPs beneath an S3 prefix."""
    paginator = s3.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        objects.extend(
            S3Object(item["Key"], item["Size"])
            for item in page.get("Contents", [])
            if item["Key"].endswith(".zip")
        )
    return objects


def build_object_tree(objects: list[S3Object]) -> ObjectNode:
    root = ObjectNode(S3_BUCKET)
    for item in sorted(objects, key=lambda entry: entry.key):
        current = root
        for part in item.key.split("/"):
            current = current.children.setdefault(part, ObjectNode(part))
        current.object_size = item.size
    return root


class TranscriptBrowser(App[None]):
    """Explore transcript folders and objects without downloading them."""

    CSS = """
    Tree {
        height: 1fr;
        border: round $accent;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]

    def __init__(self, objects: list[S3Object]) -> None:
        super().__init__()
        self._objects = objects

    def compose(self) -> ComposeResult:
        model = build_object_tree(self._objects)
        tree: Tree[None] = Tree("", id="transcripts")
        tree.root.expand()
        self._populate(tree.root, model)
        yield Header()
        yield tree
        yield Footer()

    def _populate(self, parent: TreeNode[None], model: ObjectNode) -> None:
        children = sorted(
            model.children.values(),
            key=lambda child: (child.object_size is not None, child.name.lower()),
        )
        for child in children:
            tree_node = (
                parent.add_leaf(self._label(child))
                if child.object_size is not None
                else parent.add(self._label(child))
            )
            self._populate(tree_node, child)

    @staticmethod
    def _label(node: ObjectNode) -> Text:
        count, size = node.summary()
        detail = (
            human_size(size)
            if node.object_size is not None
            else f"{count} transcripts, {human_size(size)}"
        )
        return Text.assemble((node.name, "cyan"), (f"  {detail}", "dim"))

    def on_mount(self) -> None:
        self.title = "rr-trans"
        self.sub_title = "enter=expand · q=quit"
        tree = self.query_one("#transcripts", Tree)
        model = build_object_tree(self._objects)
        tree.root.set_label(self._label(model))
        tree.focus()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rr-trans",
        description="Browse uploaded transcript folders in S3.",
    )
    parser.add_argument(
        "--prefix",
        default=f"{STORAGE_PREFIX}/",
        help=f"S3 prefix to browse (default: {STORAGE_PREFIX}/).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    print(f"Listing s3://{S3_BUCKET}/{args.prefix} ...", file=sys.stderr)
    objects = list_objects(make_s3_client(), args.prefix)
    if not objects:
        print("No transcripts found.", file=sys.stderr)
        return 1
    TranscriptBrowser(objects).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
