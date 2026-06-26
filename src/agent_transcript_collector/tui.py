"""Interactive terminal selector for choosing which sources to download.

Optional — requires the ``tui`` extra (Textual). :func:`select_sources` shows a
checkbox list of the sources present in the bucket (with unit counts and sizes),
and returns the source ids the user picked, or ``None`` if they cancelled.

Selection is at the source level on purpose: it's the one segment that is
human-meaningful across every key layout in the bucket. For finer slices
(a single contributor or prefix) use the CLI's ``--contributor`` / ``--prefix``.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, SelectionList
from textual.widgets.selection_list import Selection

from .catalog import Aggregate


def _build_selections(source_aggs: dict[str, Aggregate]) -> list[Selection]:
    selections: list[Selection] = []
    for source, agg in source_aggs.items():
        prompt = f"{source}  ({agg.count} zips, {agg.size_human})"
        selections.append(Selection(prompt, source, False))
    return selections


class SourceSelector(App[list[str] | None]):
    """Check the sources to download; press ``d`` to confirm, ``q`` to cancel."""

    CSS = """
    SelectionList {
        height: 1fr;
        border: round $accent;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("a", "all", "All"),
        Binding("n", "none", "None"),
        Binding("d", "confirm", "Download"),
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, source_aggs: dict[str, Aggregate]) -> None:
        super().__init__()
        self._source_aggs = source_aggs

    def compose(self) -> ComposeResult:
        yield Header()
        yield SelectionList[str](*_build_selections(self._source_aggs), id="sources")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Transcript downloader"
        self.sub_title = "space toggles · a=all · n=none · d=download · q=cancel"
        self.query_one(SelectionList).focus()

    def action_all(self) -> None:
        self.query_one(SelectionList).select_all()

    def action_none(self) -> None:
        self.query_one(SelectionList).deselect_all()

    def action_confirm(self) -> None:
        self.exit(list(self.query_one(SelectionList).selected))

    def action_cancel(self) -> None:
        self.exit(None)


def select_sources(source_aggs: dict[str, Aggregate]) -> list[str] | None:
    """Run the selector. Returns chosen source ids, or ``None`` if cancelled/empty."""
    if not source_aggs:
        return None
    return SourceSelector(source_aggs).run()
