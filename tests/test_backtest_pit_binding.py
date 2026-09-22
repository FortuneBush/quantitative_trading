from datetime import date, datetime, timezone

import pytest

from quant_monitor.backtesting import BacktestConfig, BacktestEngine, BacktestValidationError
from quant_monitor.domain import Bar, SHANGHAI_TZ
from quant_monitor.research import PointInTimeUniverse, UniverseMembership
from quant_monitor.strategies import Strategy


class _Noop(Strategy):
    @property
    def name(self):
        return "pit_noop"

    def evaluate(self, context):
        return ()


def _bar(symbol, day):
    timestamp = datetime(2025, 1, 6 + day, 15, tzinfo=SHANGHAI_TZ)
    return Bar(
        symbol=symbol,
        interval="1d",
        timestamp=timestamp,
        open="10",
        high="10",
        low="10",
        close="10",
        volume=100000,
        turnover="1000000",
        source="fixture",
    )


def _membership(symbol, *, start=date(2025, 1, 6), active=True):
    return UniverseMembership(
        universe="banks",
        symbol=symbol,
        effective_from=start,
        effective_to=None,
        known_at=datetime(2025, 1, 1, 8, tzinfo=timezone.utc),
        active=active,
        exchange=symbol.rsplit(".", 1)[1],
        security_type="stock",
        industry="银行",
        source="fixture",
        version="banks-v1",
        revision_id="r1",
    )


def test_backtest_binds_every_bar_to_point_in_time_membership_and_records_hashes():
    universe = PointInTimeUniverse([_membership("600000.SH")], version="banks-v1")
    result = BacktestEngine(_Noop(), BacktestConfig()).run(
        {"600000.SH": [_bar("600000.SH", 0), _bar("600000.SH", 1)]},
        point_in_time_universe=universe,
        universe_knowledge_cutoff=datetime(2025, 1, 2, 8, tzinfo=timezone.utc),
    )
    binding = result.diagnostics.assumptions["universe_binding"]
    assert binding["mode"] == "point_in_time"
    assert binding["version"] == "banks-v1"
    assert len(binding["snapshot_hashes"]) == 2
    # The binding remains structured evidence in the immutable result rather
    # than a lossy string representation.  Both levels are read-only.
    assert isinstance(binding["snapshot_hashes"], tuple)
    with pytest.raises(TypeError):
        binding["mode"] = "detached"


def test_backtest_rejects_future_listing_bar_instead_of_survivorship_bias():
    universe = PointInTimeUniverse(
        [_membership("600000.SH"), _membership("600001.SH", start=date(2025, 1, 7))],
        version="banks-v1",
    )
    with pytest.raises(BacktestValidationError, match="outside point-in-time universe"):
        BacktestEngine(_Noop()).run(
            {"600001.SH": [_bar("600001.SH", 0), _bar("600001.SH", 1)]},
            point_in_time_universe=universe,
            universe_knowledge_cutoff=datetime(2025, 1, 2, 8, tzinfo=timezone.utc),
        )


def test_point_in_time_resolver_requires_explicit_knowledge_cutoff():
    universe = PointInTimeUniverse([_membership("600000.SH")], version="banks-v1")
    with pytest.raises(BacktestValidationError, match="knowledge_cutoff"):
        BacktestEngine(_Noop()).run(
            {"600000.SH": [_bar("600000.SH", 0)]},
            point_in_time_universe=universe,
        )


def test_backtest_rejects_knowledge_cutoff_after_earliest_decision_timestamp():
    """A shared PIT cutoff must not introduce information from the future."""

    universe = PointInTimeUniverse([_membership("600000.SH")], version="banks-v1")
    # 2025-01-06 16:00 Shanghai is after the first bar's 15:30 decision
    # timestamp.  The resolver can answer this as-of query for inspection, but
    # a backtest must fail closed instead of leaking later knowledge.
    future_cutoff = datetime(2025, 1, 6, 16, tzinfo=SHANGHAI_TZ)
    with pytest.raises(BacktestValidationError, match="earliest backtest decision"):
        BacktestEngine(_Noop(), BacktestConfig()).run(
            {"600000.SH": [_bar("600000.SH", 0)]},
            point_in_time_universe=universe,
            universe_knowledge_cutoff=future_cutoff,
        )


def test_backtest_allows_cutoff_at_earliest_decision_timestamp():
    universe = PointInTimeUniverse([_membership("600000.SH")], version="banks-v1")
    decision_cutoff = datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ)
    result = BacktestEngine(_Noop(), BacktestConfig()).run(
        {"600000.SH": [_bar("600000.SH", 0)]},
        point_in_time_universe=universe,
        universe_knowledge_cutoff=decision_cutoff,
    )
    assert result.diagnostics.assumptions["universe_binding"]["mode"] == "point_in_time"
