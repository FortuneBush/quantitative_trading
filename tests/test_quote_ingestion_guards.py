"""Fail-closed guards for normalized quote snapshots."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.domain import Quote
from quant_monitor.services import EventBus, MarketDataCollectionService
from quant_monitor.storage import SQLiteRepository


AS_OF = datetime.fromisoformat("2026-09-15T10:00:00+08:00")


def _quote(**overrides):
    values = {
        "symbol": "600519.SH",
        "timestamp": AS_OF,
        "last": Decimal("100"),
        "bid": Decimal("99.9"),
        "ask": Decimal("100.1"),
        "source": "fixture",
    }
    values.update(overrides)
    return Quote(**values)


def test_quote_rejects_crossed_two_sided_book():
    with pytest.raises(ValueError, match="bid cannot be above ask"):
        _quote(bid=Decimal("100.2"), ask=Decimal("100.1"))


def test_quote_allows_one_sided_book():
    quote = _quote(bid=Decimal("99.9"), ask=None)
    assert quote.bid == Decimal("99.9")
    assert quote.ask is None


def test_collection_rejects_duplicate_symbol_snapshot_without_persisting(tmp_path):
    class DuplicateProvider:
        name = "fixture"

        async def fetch_quotes(self, symbols):
            return [_quote(), _quote(timestamp=AS_OF.replace(second=1))]

    async def exercise():
        repository = SQLiteRepository(tmp_path / "quotes.sqlite3")
        repository.initialize()
        collector = MarketDataCollectionService(
            provider=DuplicateProvider(),
            repository=repository,
            event_bus=EventBus(),
            collect_bars=False,
        )
        try:
            with pytest.raises(ValueError, match="duplicate quotes"):
                await collector.collect_quotes(["600519.SH"])
            assert repository.get_latest_quotes(["600519.SH"]) == []
        finally:
            repository.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["none", "mapping", "foreign", "unrequested", "future"])
def test_collection_rejects_malformed_snapshot_before_write_or_event(tmp_path, kind):
    class InvalidProvider:
        name = "fixture"

        async def fetch_quotes(self, symbols):
            return {
                "none": None,
                "mapping": [{"symbol": "600519.SH"}],
                "foreign": [_quote(source="fixture-lookalike")],
                "unrequested": [_quote(symbol="000001.SZ")],
                "future": [_quote(timestamp=AS_OF + timedelta(seconds=6))],
            }[kind]

    async def exercise():
        repository = SQLiteRepository(tmp_path / "invalid.sqlite3")
        repository.initialize()
        bus = EventBus()
        events = []
        await bus.subscribe(None, events.append)
        collector = MarketDataCollectionService(
            InvalidProvider(), repository, bus, clock=lambda: AS_OF
        )
        try:
            with pytest.raises(ValueError):
                await collector.collect_quotes(["600519.SH"])
            assert repository.get_latest_quotes() == []
            assert events == []
        finally:
            repository.close()

    asyncio.run(exercise())


def test_overlapping_snapshot_responses_publish_only_monotonic_committed_data(tmp_path):
    class OutOfOrderProvider:
        name = "fixture"

        def __init__(self):
            self.first = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch_quotes(self, symbols):
            if not self.first.is_set():
                self.first.set()
                await self.release.wait()
                return [_quote()]
            return [_quote(timestamp=AS_OF + timedelta(seconds=1))]

    async def exercise():
        repository = SQLiteRepository(tmp_path / "concurrent.sqlite3")
        repository.initialize()
        provider = OutOfOrderProvider()
        bus = EventBus(raise_handler_errors=True)
        events = []

        def observed(event):
            assert repository.get_latest_quotes()[0] == event.payload
            events.append(event)

        await bus.subscribe(None, observed)
        collector = MarketDataCollectionService(
            provider, repository, bus, clock=lambda: AS_OF + timedelta(seconds=2)
        )
        slow = asyncio.create_task(collector.collect_quote_snapshot(["600519.SH"], relaxed=True))
        try:
            await asyncio.wait_for(provider.first.wait(), 1)
            newest = await collector.collect_quote_snapshot(["600519.SH"], relaxed=True)
            provider.release.set()
            late = await asyncio.wait_for(slow, 1)
            assert late.quotes == ()
            assert late.stale_symbols == ("600519.SH",)
            assert len(events) == 1
            assert repository.get_latest_quotes() == list(newest.quotes)
            assert repository.get_quotes("600519.SH") == list(newest.quotes)
        finally:
            provider.release.set()
            await asyncio.gather(slow, return_exceptions=True)
            repository.close()

    asyncio.run(exercise())


def test_collection_rejects_stale_quote_without_overwriting_latest(tmp_path):
    class SequenceProvider:
        name = "fixture"

        def __init__(self):
            self.current = _quote()

        async def fetch_quotes(self, symbols):
            return [self.current]

    async def exercise():
        repository = SQLiteRepository(tmp_path / "stale.sqlite3")
        repository.initialize()
        provider = SequenceProvider()
        collector = MarketDataCollectionService(
            provider=provider,
            repository=repository,
            event_bus=EventBus(),
            collect_bars=False,
        )
        try:
            await collector.collect_quotes(["600519.SH"])
            latest = _quote(
                timestamp=AS_OF.replace(second=2),
                last=Decimal("101"),
                bid=Decimal("100.9"),
                ask=Decimal("101.1"),
            )
            provider.current = latest
            await collector.collect_quotes(["600519.SH"])
            provider.current = _quote(timestamp=AS_OF.replace(second=1))
            with pytest.raises(ValueError, match="stale quotes"):
                await collector.collect_quotes(["600519.SH"])
            assert repository.get_latest_quotes(["600519.SH"])[0] == latest
        finally:
            repository.close()

    asyncio.run(exercise())
