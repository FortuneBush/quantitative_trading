"""Atomic, source-bound live snapshot persistence without real providers."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import threading

import pytest

from quant_monitor.domain import Quote, parse_market_time
from quant_monitor.storage import SQLiteRepository


BASE_TIME = parse_market_time("2026-08-28T10:00:00+08:00")


def quote(symbol="600036.SH", *, seconds=0, source="mock", price="10"):
    return Quote(
        symbol=symbol,
        timestamp=BASE_TIME + timedelta(seconds=seconds),
        last=Decimal(price),
        source=source,
    )


@pytest.fixture
def repository(tmp_path):
    with SQLiteRepository(tmp_path / "snapshots.sqlite3") as instance:
        yield instance


def test_strict_snapshot_rejects_stale_and_new_rows_as_one_batch(repository):
    previous = quote(seconds=10)
    repository.upsert_quotes([previous])
    stale = quote(seconds=9)
    new = quote("600519.SH", seconds=11)

    with pytest.raises(ValueError, match="provider returned stale quotes"):
        repository.upsert_quote_snapshot([stale, new], source_prefix="mock")

    assert repository.get_quotes(previous.symbol) == [previous]
    assert repository.get_quotes(new.symbol) == []


def test_relaxed_snapshot_skips_only_older_rows_and_returns_accepted_order(repository):
    previous = quote(seconds=10)
    repository.upsert_quotes([previous])
    stale = quote(seconds=9)
    fresh = quote("600519.SH", seconds=11)
    another = quote("601318.SH", seconds=12)

    accepted = repository.upsert_quote_snapshot(
        [fresh, stale, another], source_prefix="mock", reject_stale=False
    )

    assert accepted == [fresh, another]
    assert repository.get_quotes(previous.symbol) == [previous]
    assert repository.get_quotes(fresh.symbol) == [fresh]
    assert repository.get_quotes(another.symbol) == [another]


def test_relaxed_snapshot_all_stale_is_a_noop(repository):
    previous = quote(seconds=10)
    repository.upsert_quotes([previous])
    assert repository.upsert_quote_snapshot(
        [quote(seconds=9)], source_prefix="mock", reject_stale=False
    ) == []
    assert repository.get_quotes(previous.symbol) == [previous]


@pytest.mark.parametrize("invalid", [None, {}, "not a quote", object()])
def test_invalid_payload_row_does_not_partially_write(repository, invalid):
    with pytest.raises(ValueError, match="Quote instances"):
        repository.upsert_quote_snapshot([quote(), invalid], source_prefix="mock")
    assert repository.get_latest_quotes() == []


@pytest.mark.parametrize("prefix", [None, "", "   ", 42])
def test_invalid_source_prefix_does_not_write(repository, prefix):
    with pytest.raises(ValueError, match="source_prefix"):
        repository.upsert_quote_snapshot([quote()], source_prefix=prefix)
    assert repository.get_latest_quotes() == []


@pytest.mark.parametrize("source", ["", " ", "other", "mockery", "mock_data", "mock-other"])
def test_mixed_or_lookalike_sources_reject_whole_batch(repository, source):
    with pytest.raises(ValueError, match="unexpected source"):
        repository.upsert_quote_snapshot(
            [quote(), quote("600519.SH", source=source)], source_prefix="mock"
        )
    assert repository.get_latest_quotes() == []


@pytest.mark.parametrize("reject_stale", [True, False])
def test_duplicate_symbols_reject_whole_snapshot(repository, reject_stale):
    with pytest.raises(ValueError, match="duplicate"):
        repository.upsert_quote_snapshot(
            [quote(), quote(seconds=1)],
            source_prefix="mock",
            reject_stale=reject_stale,
        )
    assert repository.get_latest_quotes() == []


def test_snapshot_sources_are_case_insensitive_and_allow_colon_suffix(repository):
    previous = quote(seconds=10, source="MoCk:realtime")
    repository.upsert_quotes([previous])
    with pytest.raises(ValueError, match="provider returned stale quotes"):
        repository.upsert_quote_snapshot(
            [quote(seconds=9, source="MOCK:snapshot")], source_prefix="MoCk"
        )
    current = quote(seconds=11, source="mock")
    assert repository.upsert_quote_snapshot([current], source_prefix="MOCK") == [current]


def test_latest_check_is_bound_to_literal_source_prefix(repository):
    own = quote(seconds=5, source="mock_%:snapshot")
    lookalike = quote(seconds=20, source="mockXY:snapshot")
    unrelated = quote(seconds=30, source="different")
    repository.upsert_quotes([own, lookalike, unrelated])
    fresh = quote(seconds=6, source="mock_%")

    assert repository.upsert_quote_snapshot([fresh], source_prefix="mock_%") == [fresh]
    with pytest.raises(ValueError, match="provider returned stale quotes"):
        repository.upsert_quote_snapshot(
            [quote(seconds=4, source="mock_%")], source_prefix="mock_%"
        )
    assert repository.get_quotes(own.symbol) == [own, fresh, lookalike, unrelated]


def test_same_timestamp_correction_is_accepted(repository):
    previous = quote()
    correction = replace(previous, last=Decimal("10.25"), volume=500)
    repository.upsert_quotes([previous])

    assert repository.upsert_quote_snapshot([correction], source_prefix="mock") == [correction]
    assert repository.get_quotes(previous.symbol) == [correction]


def test_historical_upsert_still_accepts_older_rows(repository):
    newest = quote(seconds=20)
    oldest = quote(seconds=0)
    middle = quote(seconds=10)
    repository.upsert_quote_snapshot([newest], source_prefix="mock")

    assert repository.upsert_quotes([middle, oldest]) == 2
    assert repository.get_quotes(newest.symbol) == [oldest, middle, newest]
    assert repository.get_latest_quotes() == [newest]


def test_empty_snapshot_is_a_noop(repository):
    assert repository.upsert_quote_snapshot([], source_prefix="mock") == []
    assert repository.get_latest_quotes() == []


def test_snapshot_check_and_write_cannot_interleave_with_another_writer(repository, monkeypatch):
    previous = quote(seconds=0)
    snapshot = quote(seconds=10)
    concurrent = quote(seconds=20)
    repository.upsert_quotes([previous])
    reached_write = threading.Event()
    allow_write = threading.Event()
    other_attempted_lock = threading.Event()
    other_acquired_lock = threading.Event()
    failures = []
    original_upsert = repository.upsert_quotes
    original_lock = repository._lock

    class ObservedLock:
        def __enter__(self):
            is_other = threading.current_thread().name == "snapshot-other-writer"
            if is_other:
                other_attempted_lock.set()
            original_lock.acquire()
            if is_other:
                other_acquired_lock.set()
            return self

        def __exit__(self, *_):
            original_lock.release()

    def gated_upsert(rows):
        # Pause precisely after the snapshot freshness query, before SQL
        # insertion. A competing ordinary history writer uses the same lock.
        if threading.current_thread().name == "snapshot-writer":
            reached_write.set()
            if not allow_write.wait(5):
                raise AssertionError("test did not release snapshot writer")
        return original_upsert(rows)

    def persist_snapshot():
        try:
            assert repository.upsert_quote_snapshot([snapshot], source_prefix="mock") == [snapshot]
        except BaseException as exc:
            failures.append(exc)

    def persist_other():
        try:
            original_upsert([concurrent])
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(repository, "_lock", ObservedLock())
    monkeypatch.setattr(repository, "upsert_quotes", gated_upsert)
    snapshot_thread = threading.Thread(target=persist_snapshot, name="snapshot-writer")
    other_thread = threading.Thread(target=persist_other, name="snapshot-other-writer")
    snapshot_thread.start()
    try:
        assert reached_write.wait(5)
        other_thread.start()
        assert other_attempted_lock.wait(5)
        assert not other_acquired_lock.wait(0.05), "writer interleaved between check and insert"
    finally:
        allow_write.set()
        snapshot_thread.join(5)
        if other_thread.ident is not None:
            other_thread.join(5)
    assert not snapshot_thread.is_alive()
    assert not other_thread.is_alive()
    assert not failures
    assert other_acquired_lock.is_set()
    assert repository.get_quotes(previous.symbol) == [previous, snapshot, concurrent]
    assert repository.get_latest_quotes() == [concurrent]
