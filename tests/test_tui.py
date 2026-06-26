import asyncio

import pytest

pytest.importorskip("textual")

from agent_transcript_collector.catalog import Aggregate
from agent_transcript_collector.tui import SourceSelector, select_sources


def test_select_all_then_confirm_returns_all_sources():
    app = SourceSelector({"claude_code": Aggregate(10, 1000), "codex": Aggregate(2, 50)})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("a")  # select all
            await pilot.pause()
            await pilot.press("d")  # confirm/download
            await pilot.pause()

    asyncio.run(scenario())
    assert sorted(app.return_value) == ["claude_code", "codex"]


def test_cancel_returns_none():
    app = SourceSelector({"codex": Aggregate(1, 1)})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("q")  # cancel
            await pilot.pause()

    asyncio.run(scenario())
    assert app.return_value is None


def test_select_sources_empty_catalog_returns_none():
    assert select_sources({}) is None
