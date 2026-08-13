"""Lazy S3 transcript browser for the ``rr-trans`` command."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Tree
from textual.widgets.tree import TreeNode

from .atif import ATIF_FILENAME
from .s3client import S3_BUCKET, make_s3_client
from .storage import STORAGE_PREFIX


@dataclass(frozen=True)
class S3Entry:
    """One immediate child of an S3 prefix."""

    name: str
    key: str
    is_folder: bool
    last_modified: datetime | None = None


def list_folder(s3, prefix: str) -> list[S3Entry]:
    """List only the immediate folders and transcript ZIPs under ``prefix``."""
    paginator = s3.get_paginator("list_objects_v2")
    folders: dict[str, S3Entry] = {}
    objects: dict[str, S3Entry] = {}
    for page in paginator.paginate(
        Bucket=S3_BUCKET,
        Prefix=prefix,
        Delimiter="/",
    ):
        for item in page.get("CommonPrefixes", []):
            key = item["Prefix"]
            name = key.removeprefix(prefix).rstrip("/")
            if name:
                folders[key] = S3Entry(name=name, key=key, is_folder=True)
        for item in page.get("Contents", []):
            key = item["Key"]
            name = key.removeprefix(prefix)
            if name and "/" not in name and key.endswith(".zip"):
                objects[key] = S3Entry(
                    name=name,
                    key=key,
                    is_folder=False,
                    last_modified=item["LastModified"],
                )
    return [
        *sorted(folders.values(), key=lambda entry: entry.name.lower()),
        *sorted(
            objects.values(),
            key=lambda entry: (
                -entry.last_modified.timestamp() if entry.last_modified else 0,
                entry.name.lower(),
            ),
        ),
    ]


def entry_label(entry: S3Entry) -> Text:
    """Build a tree label, including upload time for transcript objects."""
    label = Text(entry.name, style="cyan" if entry.is_folder else "")
    if entry.last_modified is not None:
        modified = entry.last_modified.astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        label.append(f"  {modified}", style="dim")
    return label


class TranscriptBrowser(App[None]):
    """Explore transcript folders, loading one S3 level when it is opened."""

    CSS = """
    Tree {
        height: 1fr;
        border: round $accent;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("v", "view_atif", "View ATIF in Vim"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix
        self._s3 = None
        self._loading: set[int] = set()
        self._loaded: set[int] = set()

    def compose(self) -> ComposeResult:
        tree: Tree[S3Entry] = Tree("Connecting to S3…", id="transcripts")
        tree.root.expand()
        yield Header()
        yield tree
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Redwood Research Transcript Collector"
        self.sub_title = "enter=expand · v=view ATIF in Vim · q=quit"
        tree = self.query_one("#transcripts", Tree)
        tree.focus()
        self._load(tree.root, self._prefix)

    def action_view_atif(self) -> None:
        """Download the selected transcript's ATIF and open it temporarily."""
        entry = self.query_one("#transcripts", Tree).cursor_node.data
        if entry is None or entry.is_folder:
            self.notify(
                "Select a transcript ZIP first.",
                title="View ATIF",
                severity="warning",
            )
            return

        self.notify("Downloading ATIF…", title="View ATIF")
        self.run_worker(
            lambda: self._download_and_view_atif(entry),
            thread=True,
            exit_on_error=False,
            exclusive=True,
            group="atif-viewer",
        )

    def _download_and_view_atif(self, entry: S3Entry) -> None:
        try:
            if self._s3 is None:
                self._s3 = make_s3_client()
            with tempfile.TemporaryDirectory(prefix="rr-trans-atif-view-") as temporary:
                directory = Path(temporary)
                os.chmod(directory, 0o700)
                archive_path = directory / "transcript.zip"
                atif_path = directory / ATIF_FILENAME
                self._s3.download_file(S3_BUCKET, entry.key, str(archive_path))
                with zipfile.ZipFile(archive_path) as archive:
                    try:
                        with archive.open(ATIF_FILENAME) as source, atif_path.open(
                            "wb"
                        ) as target:
                            shutil.copyfileobj(source, target)
                    except KeyError as exc:
                        raise ValueError(
                            "This upload does not contain an ATIF file."
                        ) from exc
                atif_path.chmod(0o600)
                archive_path.unlink()
                self.call_from_thread(self._open_vim, atif_path)
        except Exception as exc:  # noqa: BLE001 - report S3, ZIP, and editor errors
            self.call_from_thread(
                self.notify,
                str(exc),
                title="Unable to view ATIF",
                severity="error",
            )

    def _open_vim(self, atif_path: Path) -> None:
        with self.suspend():
            subprocess.run(
                [
                    "vim",
                    "-N",
                    "-u",
                    "NONE",
                    "-i",
                    "NONE",
                    "-n",
                    "-R",
                    "--",
                    str(atif_path),
                ],
                check=False,
            )

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[S3Entry]) -> None:
        entry = event.node.data
        if entry is not None and entry.is_folder:
            self._load(event.node, entry.key)

    def _load(self, node: TreeNode[S3Entry], prefix: str) -> None:
        node_id = node.id
        if node_id in self._loading or node_id in self._loaded:
            return
        self._loading.add(node_id)
        node.remove_children()
        node.add_leaf(Text("Loading…", style="dim"))

        def fetch() -> None:
            try:
                if self._s3 is None:
                    self._s3 = make_s3_client()
                entries = list_folder(self._s3, prefix)
            except Exception as exc:
                self.call_from_thread(self._show_error, node, exc)
            else:
                self.call_from_thread(self._show_entries, node, entries)

        self.run_worker(
            fetch,
            thread=True,
            exit_on_error=False,
            group="s3-listing",
        )

    def _show_entries(
        self,
        node: TreeNode[S3Entry],
        entries: list[S3Entry],
    ) -> None:
        self._loading.discard(node.id)
        self._loaded.add(node.id)
        node.remove_children()
        if node is self.query_one("#transcripts", Tree).root:
            node.set_label(Text(f"s3://{S3_BUCKET}/{self._prefix}", style="cyan"))
        if not entries:
            node.add_leaf(Text("No transcripts found", style="dim"))
            return
        for entry in entries:
            label = entry_label(entry)
            if entry.is_folder:
                child = node.add(label, data=entry)
                child.add_leaf(Text("Open to load", style="dim"))
            else:
                node.add_leaf(label, data=entry)

    def _show_error(self, node: TreeNode[S3Entry], error: Exception) -> None:
        self._loading.discard(node.id)
        node.remove_children()
        node.add_leaf(Text(f"Unable to load: {error}", style="red"))


def main(prefix: str = f"{STORAGE_PREFIX}/") -> int:
    normalized = prefix.rstrip("/") + "/"
    TranscriptBrowser(normalized).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
