"""Interactive S3 archive browser for ``rr-trans --tui``."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Tree
from textual.widgets.tree import TreeNode

from .catalog import Unit
from .sources.base import human_size


@dataclass
class CatalogNode:
    """One folder or archive in the S3 key hierarchy."""

    name: str
    children: dict[str, "CatalogNode"] = field(default_factory=dict)
    unit: Unit | None = None

    def units(self) -> list[Unit]:
        if self.unit is not None:
            return [self.unit]
        return [
            unit
            for child in self.children.values()
            for unit in child.units()
        ]


def build_catalog_tree(units: list[Unit]) -> CatalogNode:
    root = CatalogNode("rr-agent-transcripts")
    for unit in sorted(units, key=lambda item: item.key):
        current = root
        for part in unit.key.split("/"):
            current = current.children.setdefault(part, CatalogNode(part))
        current.unit = unit
    return root


@dataclass(frozen=True)
class TreeItem:
    name: str
    keys: tuple[str, ...]
    size: int
    archive: bool


def _tree_item(node: CatalogNode) -> TreeItem:
    units = node.units()
    return TreeItem(
        name=node.name,
        keys=tuple(unit.key for unit in units),
        size=sum(unit.size for unit in units),
        archive=node.unit is not None,
    )


class ArchiveSelector(App[list[Unit] | None]):
    """Browse folders and select archive subtrees to download."""

    CSS = """
    Tree {
        height: 1fr;
        border: round $accent;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_selection", "Select", priority=True),
        Binding("a", "all", "All"),
        Binding("n", "none", "None"),
        Binding("d", "confirm", "Download"),
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, units: list[Unit]) -> None:
        super().__init__()
        self._units = units
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        model = build_catalog_tree(self._units)
        tree: Tree[TreeItem] = Tree("", data=_tree_item(model), id="archives")
        tree.root.expand()
        self._populate(tree.root, model)
        yield Header()
        yield tree
        yield Footer()

    def _populate(self, parent: TreeNode[TreeItem], model: CatalogNode) -> None:
        children = sorted(
            model.children.values(),
            key=lambda child: (child.unit is not None, child.name.lower()),
        )
        for child in children:
            item = _tree_item(child)
            tree_node = (
                parent.add_leaf("", data=item)
                if item.archive
                else parent.add("", data=item)
            )
            self._set_label(tree_node)
            self._populate(tree_node, child)

    def on_mount(self) -> None:
        self.title = "rr-trans"
        self.sub_title = (
            "enter=expand · space=select · a=all · n=none · "
            "d=download · q=cancel"
        )
        tree = self.query_one("#archives", Tree)
        self._set_label(tree.root)
        tree.focus()

    def _set_label(self, node: TreeNode[TreeItem]) -> None:
        item = node.data
        if item is None:
            return
        selected = sum(key in self._selected for key in item.keys)
        marker = "[x]" if selected == len(item.keys) else "[-]" if selected else "[ ]"
        detail = (
            human_size(item.size)
            if item.archive
            else f"{len(item.keys)} archives, {human_size(item.size)}"
        )
        node.set_label(
            Text.assemble(
                (f"{marker} ", "bold cyan"),
                item.name,
                (f"  {detail}", "dim"),
            )
        )

    def _refresh_labels(self, node: TreeNode[TreeItem] | None = None) -> None:
        node = node or self.query_one("#archives", Tree).root
        self._set_label(node)
        for child in node.children:
            self._refresh_labels(child)

    def action_toggle_selection(self) -> None:
        node = self.query_one("#archives", Tree).cursor_node
        if node is None or node.data is None:
            return
        keys = set(node.data.keys)
        if keys and keys <= self._selected:
            self._selected -= keys
        else:
            self._selected |= keys
        self._refresh_labels()

    def action_all(self) -> None:
        self._selected = {unit.key for unit in self._units}
        self._refresh_labels()

    def action_none(self) -> None:
        self._selected.clear()
        self._refresh_labels()

    def action_confirm(self) -> None:
        self.exit([unit for unit in self._units if unit.key in self._selected])

    def action_cancel(self) -> None:
        self.exit(None)


def select_units(units: list[Unit]) -> list[Unit] | None:
    """Run the archive browser and return its selected units."""
    if not units:
        return None
    return ArchiveSelector(units).run()
