"""Offline HTTP regression coverage for the bank-model projection contract."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import Bar, FundamentalObservation, Quote
from quant_monitor.strategies import (
    BankDailyMultifactorStrategy,
    CohortStrategyContext,
    StrategyContext,
)


AS_OF = datetime.fromisoformat("2026-09-14T15:00:00+08:00")
SYMBOLS = ("600036.SH", "000001.SZ", "601398.SH")
FACTOR_KEYS = (
    "momentum_60_5",
    "momentum_120_20",
    "annualized_volatility_60",
    "price_to_sma_120",
    "sma_20_to_60",
    "max_drawdown_60",
)


def daily_bars(symbol, index=0, count=130):
    """Build positive-volume, weekday-only bars with distinct factor scores."""
    dates = []
    cursor = AS_OF
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    rows = []
    for offset, timestamp in enumerate(reversed(dates)):
        price = (10 + index) * math.exp(
            offset * (0.0005 + index * 0.0002)
            + math.sin(offset * 0.6) * (0.001 + index * 0.001)
        )
        close = Decimal(str(price))
        rows.append(
            Bar(
                symbol=symbol,
                interval="1d",
                timestamp=timestamp,
                open=close * Decimal("0.999"),
                high=close * Decimal("1.01"),
                low=close * Decimal("0.99"),
                close=close,
                volume=1_000_000 + index * 100_000,
                turnover=close * Decimal(1_000_000 + index * 100_000),
                source="mock",
            )
        )
    return tuple(rows)


@pytest.fixture
def cached_bank_client(tmp_path, monkeypatch):
    # Starting a real runtime exercises database/lifespan behavior, but priming
    # is deliberately empty. All research fixtures are written to this temp DB.
    async def empty_data(*args, **kwargs):
        return []

    for method in ("fetch_quotes", "fetch_bars", "fetch_instruments"):
        monkeypatch.setattr(MockMarketDataProvider, method, empty_data)
    monkeypatch.setattr(
        "quant_monitor.app.expected_latest_daily_date", lambda: AS_OF.date()
    )
    settings = Settings(
        database_path=str(tmp_path / "bank-contract.sqlite3"),
        data_provider="mock",
        watchlist=list(SYMBOLS),
        poll_interval_seconds=3600,
        history_bar_count=130,
        bank_strategy_min_universe=2,
        bank_strategy_entry_percentile=0.72,
        bank_strategy_exit_percentile=0.34,
        bank_strategy_max_volatility=0.32,
        bank_strategy_max_drawdown=0.12,
    )
    with TestClient(create_app(settings)) as client:
        calls = []

        async def no_provider_io(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("bank snapshot must only read cached domain data")

        for method in ("fetch_quotes", "fetch_bars", "fetch_instruments"):
            monkeypatch.setattr(client.app.state.runtime.provider, method, no_provider_io)
        yield client
        assert calls == []


def seed_bars(client, rows_by_symbol):
    repository = client.app.state.runtime.repository
    repository.insert_bars(
        [bar for rows in rows_by_symbol.values() for bar in rows]
    )
    repository.upsert_quotes(
        [
            Quote(symbol=symbol, timestamp=AS_OF, last=rows[-1].close, source="mock")
            for symbol, rows in rows_by_symbol.items()
            if rows
        ]
    )


def strategy_snapshot(client, rows_by_symbol):
    strategy = client.app.state.runtime.monitor.cohort_strategies[0]
    snapshot = strategy.analyze(
        CohortStrategyContext(
            timestamp=AS_OF,
            members={
                symbol: StrategyContext(symbol, AS_OF, bars=rows)
                for symbol, rows in rows_by_symbol.items()
            },
            cohort="a_share_banks",
            metadata={"expected_bar_date": AS_OF.date().isoformat()},
        )
    )
    assert snapshot["ready"] is True
    return {item["symbol"]: item for item in snapshot["items"]}


def bank_fundamentals():
    from quant_monitor.research import BANK_METRIC_SPECS

    return [FundamentalObservation(
        observation_id="{}-{}".format(symbol, spec.name),
        symbol=symbol, metric=spec.name, value=Decimal("1.25"),
        unit=spec.unit, scope="consolidated",
        period_start=date(2025, 1, 1), period_end=date(2025, 12, 31),
        available_at=AS_OF, received_at=AS_OF, source="fixture", revision_id="r1",
        metadata={"internal": "must-not-be-public"},
    ) for symbol in SYMBOLS for spec in BANK_METRIC_SPECS]


def test_bank_fundamental_evidence_is_independent_of_price_history(cached_bank_client, monkeypatch):
    client = cached_bank_client
    monkeypatch.setattr("quant_monitor.app.now_market_time", lambda: AS_OF)
    client.app.state.runtime.repository.insert_fundamental_observations(bank_fundamentals())
    response = client.get("/api/v1/sectors/banks")
    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["analysis_ready"] == 0
    report = body["fundamental_factors"]
    assert report["complete_count"] == len(SYMBOLS)
    assert report["strategy_integration"] is False
    assert report["score_usage"] == "parallel_research_only"
    assert report["provider_operations"] == 0
    for item in body["items"]:
        assert item["analysis"] is None
        assert item["research_score"] is None
        assert item["research_action"] == "insufficient_data"
        fact = item["fundamental_factors"]
        assert fact["symbol"] == item["symbol"]
        assert fact["score"] == pytest.approx(0.5)
        assert fact["evidence"]["roe"]["period_end"] == "2025-12-31"
        assert fact["evidence"]["roe"]["available_at"] == AS_OF.isoformat()
        assert "metadata" not in fact["evidence"]["roe"]
    assert "must-not-be-public" not in response.text


def test_bank_fundamental_ambiguity_does_not_break_price_analysis(cached_bank_client):
    client = cached_bank_client
    rows = {symbol: daily_bars(symbol, index) for index, symbol in enumerate(SYMBOLS)}
    seed_bars(client, rows)
    expected = strategy_snapshot(client, rows)
    facts = bank_fundamentals()
    facts.append(replace(facts[0], observation_id="ambiguous", scope="standalone"))
    client.app.state.runtime.repository.insert_fundamental_observations(facts)
    response = client.get("/api/v1/sectors/banks")
    assert response.status_code == 200
    body = response.json()
    assert body["fundamental_factors"]["quality"]["status"] == "invalid"
    assert body["fundamental_factors"]["warnings"] == ["fundamental_selection_ambiguous"]
    for item in body["items"]:
        assert item["research_score"] == pytest.approx(expected[item["symbol"]]["score"])
        assert item["analysis"]["fundamental_factors"]["score"] is None


def test_bank_sector_applies_complete_fundamental_overlay_only_at_bar_decision_time(cached_bank_client):
    client = cached_bank_client
    rows = {symbol: daily_bars(symbol, index) for index, symbol in enumerate(SYMBOLS)}
    seed_bars(client, rows)
    client.app.state.runtime.repository.insert_fundamental_observations(bank_fundamentals())
    response = client.get("/api/v1/sectors/banks")
    assert response.status_code == 200
    body = response.json()
    assert body["fundamental_factors"]["strategy_integration"] is False
    assert body["fundamental_factors"]["decision_at"] == AS_OF.isoformat()
    assert body["fundamental_factors"]["quality"]["status"] == "ok"
    for item in body["items"]:
        assert item["analysis"]["fundamental_overlay"]["enabled"] is True
        assert item["analysis"]["fundamental_overlay"]["status"] == "applied"
        assert item["analysis"]["fundamental_overlay"]["decision_at"] == AS_OF.isoformat()
        assert item["fundamental_factors"]["strategy_integration"] is True
        overlay = item["analysis"]["fundamental_overlay"]
        assert item["analysis"]["trend"]["score"] == pytest.approx(
            item["analysis"]["price_score"] * (1 - overlay["weight"])
            + overlay["contribution"]
        )
        assert item["analysis"]["recommendation"]["model_action"] in {"buy", "sell", "hold"}


def test_factor_api_ignores_unrequested_symbols_and_metrics(cached_bank_client):
    client = cached_bank_client
    facts = bank_fundamentals()
    facts.extend([
        replace(facts[0], observation_id="unrelated-a", symbol="600519.SH"),
        replace(facts[0], observation_id="unrelated-b", symbol="600519.SH", scope="standalone"),
        replace(facts[0], observation_id="metric-a", metric="unrelated"),
        replace(facts[0], observation_id="metric-b", metric="unrelated", scope="standalone"),
    ])
    client.app.state.runtime.repository.insert_fundamental_observations(facts)
    response = client.get("/api/v1/fundamentals/factors/as-of", params={
        "as_of": AS_OF.isoformat(), "symbols": ",".join(SYMBOLS),
    })
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_count"] == 21
    assert body["complete_count"] == len(SYMBOLS)
    assert body["upstream_requests"] == 0


def test_bank_api_preserves_real_strategy_factors_and_units(cached_bank_client):
    client = cached_bank_client
    rows = {symbol: daily_bars(symbol, index) for index, symbol in enumerate(SYMBOLS)}
    seed_bars(client, rows)
    expected = strategy_snapshot(client, rows)

    response = client.get("/api/v1/sectors/banks")

    assert response.status_code == 200
    body = response.json()
    assert body["served_from"] == "sqlite_cache"
    assert body["upstream_requests"] == 0
    assert body["coverage"]["analysis_ready"] == len(SYMBOLS)
    # The factor model can be computed from compatibility bars for diagnosis,
    # but that must not be mistaken for evidence-ready research.
    assert body["coverage"]["quality_ready"] == 0
    assert body["coverage"]["decision_ready"] == 0
    for item in body["items"]:
        model = expected[item["symbol"]]
        analysis = item["analysis"]
        assert analysis["model_version"] == BankDailyMultifactorStrategy.MODEL_VERSION
        assert analysis["interval"] == "1d"
        for key in ("return_5", "return_20", "annualized_volatility_20"):
            assert analysis["indicators"][key] is None
        for key in FACTOR_KEYS:
            assert analysis["indicators"][key] == pytest.approx(model["factors"][key])
        assert analysis["factors"] == pytest.approx(model["factors"])
        assert analysis["factor_weights"] == model["factor_weights"]
        assert analysis["factor_percentiles"] == model["factor_percentiles"]
        assert analysis["factor_contributions"] == model["factor_contributions"]
        assert analysis["score_thresholds"] == {"entry": 0.72, "exit": 0.34}
        assert sum(analysis["factor_weights"].values()) == pytest.approx(1.0)
        assert sum(analysis["factor_contributions"].values()) == pytest.approx(
            item["research_score"]
        )
        for name, contribution in analysis["factor_contributions"].items():
            assert contribution == pytest.approx(
                analysis["factor_weights"][name] * analysis["factor_percentiles"][name]
            )
        assert analysis["rank"] == model["rank"]
        assert analysis["gates"] == model["gates"]
        assert analysis["gate_failures"] == list(model["gate_failures"]) + [
            "data_lineage_incomplete"
        ]
        assert analysis["exit_reasons"] == list(model["exit_reasons"])
        # The API adds a fail-closed provenance gate to legacy bars. Ranking
        # and the underlying pure strategy remain unchanged for diagnostics,
        # but an incomplete quality record must not surface HOLD/BUY/SELL as
        # a user-facing conclusion.
        expected_action = (
            model["action"]
            if analysis["data_quality"]["quality_complete"]
            else "insufficient_data"
        )
        assert analysis["recommendation"]["action"] == expected_action
        assert analysis["recommendation"]["model_action"] == model["action"]
        assert analysis["analysis_ready"] is True
        assert analysis["data_quality"]["analysis_ready"] is True
        # The browser readiness helper requires an explicit typed failure
        # list; a missing list would incorrectly turn even an evidence-ready
        # bank row into "研究条件未满足".
        assert analysis["data_quality"]["analysis_readiness_failures"] == [
            "immutable_observation_lineage_incomplete",
            "analysis_bar_projection_rows_without_immutable_observation",
        ]
        assert analysis["data_provenance"]["calculation_input"] == (
            "legacy_bars_projection"
        )
        assert analysis["decision_ready"] is False
        assert analysis["data_quality"]["decision_ready"] is False
        assert analysis["data_quality"]["decision_ready_reason"] == (
            "bar_projection_rows_without_immutable_observation"
        )
        assert analysis["data_quality"]["calculation_input"] == (
            "legacy_bars_projection"
        )
        if model["entry_eligible"]:
            assert analysis["recommendation"]["entry_eligible"] is False
            assert "data_lineage_incomplete" in analysis["gate_failures"]
        else:
            assert analysis["recommendation"]["entry_eligible"] is False


def test_bank_api_does_not_call_legacy_projection_authoritative_when_lineage_missing(
    cached_bank_client,
):
    """Enough cached rows must not masquerade as complete source evidence."""

    client = cached_bank_client
    rows = {symbol: daily_bars(symbol, index) for index, symbol in enumerate(SYMBOLS)}
    seed_bars(client, rows)  # compatibility bars only; no immutable observations

    body = client.get("/api/v1/sectors/banks").json()

    for item in body["items"]:
        if item["analysis"] is None:
            continue
        quality = item["analysis"]["data_quality"]
        assert quality["status"] == "partial"
        assert quality["authoritative"] is False
        assert quality["lineage_complete"] is False
        assert quality["quality_complete"] is False
        assert quality["immutable_observation_count"] == 0
        assert quality["legacy_item_count"] == quality["bars_used"]
        assert quality["non_accepted_observation_count"] == 0
        assert "bar_projection_rows_without_immutable_observation" in quality[
            "warnings"
        ]


def test_bank_api_uses_selected_strategy_thresholds_not_config_defaults(cached_bank_client):
    client = cached_bank_client
    strategy = BankDailyMultifactorStrategy(
        min_history=125,
        min_universe=3,
        entry_percentile=0.67,
        exit_percentile=0.23,
        maximum_annualized_volatility=0.02,
        maximum_drawdown=0.09,
        trend_exit_days=3,
    )
    client.app.state.runtime.monitor.cohort_strategies = (strategy,)
    rows = {symbol: daily_bars(symbol, index, 125) for index, symbol in enumerate(SYMBOLS)}
    seed_bars(client, rows)
    expected = strategy_snapshot(client, rows)

    response = client.get("/api/v1/sectors/banks")

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["history_ready"] == 3
    assert body["coverage"]["minimum_analysis_universe"] == 3
    for item in body["items"]:
        analysis = item["analysis"]
        factors = expected[item["symbol"]]["factors"]
        risk_value = max(
            factors["annualized_volatility_60"] / 0.02,
            abs(factors["max_drawdown_60"]) / 0.09,
        )
        assert analysis["score_thresholds"] == {"entry": 0.67, "exit": 0.23}
        assert analysis["data_quality"]["bars_required"] == 125
        assert analysis["risk"]["score"] == pytest.approx(min(1.0, risk_value))
        assert analysis["recommendation"]["invalidation_conditions"] == [
            "连续3个交易日收盘低于60日均线",
            "周度同业分位低于 23%",
            "年化波动高于 2% 或60日回撤达到 -9%",
        ]


def test_bank_api_excludes_foreign_sources_before_history_limit(cached_bank_client):
    client = cached_bank_client
    rows = {symbol: daily_bars(symbol, index) for index, symbol in enumerate(SYMBOLS)}
    seed_bars(client, rows)
    expected = strategy_snapshot(client, rows)
    repository = client.app.state.runtime.repository
    # Foreign rows are newer and outnumber the query window. Filtering after
    # LIMIT would erase the genuine history and silently suppress all research.
    repository.insert_bars(
        [
            replace(
                rows[symbol][-1],
                timestamp=AS_OF + timedelta(days=offset + 1),
                source="alltick",
            )
            for symbol in SYMBOLS
            for offset in range(131)
        ]
    )
    repository.upsert_quotes(
        [Quote(symbol=symbol, timestamp=AS_OF + timedelta(days=1), last="9999", source="alltick")
         for symbol in SYMBOLS]
    )

    body = client.get("/api/v1/sectors/banks").json()

    assert body["coverage"]["analysis_ready"] == 3
    for item in body["items"]:
        assert item["quote"]["source"] == "mock"
        assert item["analysis"]["bars_used"] == 130
        assert item["analysis"]["timestamp"] == AS_OF.isoformat()
        assert item["research_score"] == expected[item["symbol"]]["score"]
        assert item["analysis"]["factors"] == expected[item["symbol"]]["factors"]


def test_bank_api_does_not_fabricate_analysis_for_short_history(cached_bank_client):
    client = cached_bank_client
    rows = {
        symbol: daily_bars(symbol, index, 129 if index == 2 else 130)
        for index, symbol in enumerate(SYMBOLS)
    }
    seed_bars(client, rows)

    body = client.get("/api/v1/sectors/banks").json()

    assert body["coverage"]["history_ready"] == 2
    assert body["coverage"]["analysis_ready"] == 2
    insufficient = next(item for item in body["items"] if item["symbol"] == SYMBOLS[2])
    assert insufficient["quote"] is not None
    assert insufficient["analysis"] is None
    assert insufficient["research_score"] is None
    assert insufficient["research_action"] == "insufficient_data"
    assert insufficient["data_quality"]["analysis_ready"] is False
    assert insufficient["data_quality"]["decision_ready"] is False
    assert insufficient["data_quality"]["decision_ready_reason"] == (
        "insufficient_or_invalid_history"
    )
    assert insufficient["data_quality"]["bars_used"] == 129
    assert insufficient["data_quality"]["bars_required"] == 130
    assert insufficient["data_quality"]["reason"] == "insufficient_or_invalid_history"
