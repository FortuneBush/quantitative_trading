"""Offline persistence and API contracts for company actions and factors."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import Adjustment, AdjustmentFactor, CorporateAction, CorporateActionType, parse_market_time
from quant_monitor.storage import SQLiteRepository


RECEIVED = parse_market_time("2026-09-05T09:00:00+08:00")


def _action(action_id: str = "div-1", *, received_at=RECEIVED, amount: str = "0.35"):
    return CorporateAction(
        action_id=action_id,
        symbol="600036.SH",
        action_type=CorporateActionType.CASH_DIVIDEND,
        ex_date=date(2026, 9, 4),
        announcement_date=date(2026, 8, 20),
        cash_amount=amount,
        source="fixture-vendor",
        revision_id="r1",
        received_at=received_at,
        metadata={"fixture": True},
    )


def _factor(factor_id: str = "factor-1", *, computed_at=RECEIVED, value: str = "1.125"):
    return AdjustmentFactor(
        factor_id=factor_id,
        symbol="600036.SH",
        adjustment=Adjustment.QFQ,
        session_date=date(2026, 9, 4),
        benchmark_date=date(2026, 9, 4),
        factor=value,
        factor_version="fixture-v1",
        source="fixture-vendor",
        revision_id="r1",
        input_hash="a" * 64,
        computed_at=computed_at,
    )


def test_company_action_and_factor_storage_is_idempotent_and_append_only(tmp_path):
    repository = SQLiteRepository(tmp_path / "actions.sqlite3")
    repository.initialize()
    action = _action()
    factor = _factor()
    assert repository.insert_corporate_actions([action, action]) == 1
    assert repository.insert_adjustment_factors([factor, factor]) == 1
    assert repository.insert_corporate_actions([action]) == 0
    assert repository.insert_adjustment_factors([factor]) == 0
    with pytest.raises(ValueError, match="conflicting corporate action_id"):
        repository.insert_corporate_actions([_action(amount="0.36")])
    with pytest.raises(ValueError, match="conflicting adjustment factor_id"):
        repository.insert_adjustment_factors([_factor(value="1.25")])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository._conn().execute("DELETE FROM corporate_actions WHERE action_id = 'div-1'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository._conn().execute("UPDATE adjustment_factors SET factor = '2' WHERE factor_id = 'factor-1'")
    repository.close()


def test_action_and_factor_as_of_filters_are_point_in_time(tmp_path):
    repository = SQLiteRepository(tmp_path / "actions.sqlite3")
    repository.initialize()
    repository.insert_corporate_actions(
        [
            _action(received_at=parse_market_time("2026-09-04T09:00:00+08:00")),
            _action("div-2", received_at=parse_market_time("2026-09-06T09:00:00+08:00")),
        ]
    )
    repository.insert_adjustment_factors(
        [
            _factor(computed_at=parse_market_time("2026-09-04T09:00:00+08:00")),
            _factor("factor-2", computed_at=parse_market_time("2026-09-06T09:00:00+08:00")),
        ]
    )
    actions, total_actions = repository.query_corporate_actions(
        as_of=parse_market_time("2026-09-05T00:00:00+08:00"), symbol="600036.SH"
    )
    factors, total_factors = repository.query_adjustment_factors(
        as_of=parse_market_time("2026-09-05T00:00:00+08:00"), symbol="600036.SH"
    )
    assert total_actions == 1 and [item.action_id for item in actions] == ["div-1"]
    assert total_factors == 1 and [item.factor_id for item in factors] == ["factor-1"]
    repository.close()


def test_action_and_factor_api_are_cache_only(tmp_path):
    settings = Settings(
        app_name="actions API fixture",
        database_path=str(tmp_path / "actions-api.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=10,
        watchlist=["600036.SH"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as client:
        repository = client.app.state.runtime.repository
        repository.insert_corporate_actions([_action()])
        repository.insert_adjustment_factors([_factor()])
        actions = client.get("/api/v1/corporate-actions?symbol=600036.SH")
        factors = client.get("/api/v1/adjustment-factors?symbol=600036.SH&adjustment=qfq")
        assert actions.status_code == 200
        assert factors.status_code == 200
        assert actions.json()["provider_operations"] == 0
        assert factors.json()["provider_operations"] == 0
        assert actions.json()["items"][0]["content_hash"] == _action().content_hash
        assert factors.json()["items"][0]["content_hash"] == _factor().content_hash
        assert client.get("/api/v1/corporate-actions?action_type=invalid").status_code == 422
        assert client.get("/api/v1/adjustment-factors?as_of=2026-09-05").status_code == 422
