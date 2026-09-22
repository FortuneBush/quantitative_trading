"""Research signals remain non-authoritative across REST and local streaming."""

from decimal import Decimal

import pytest

from quant_monitor.config import Settings
from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import EventType, MarketEvent, Signal, SignalAction, now_market_time
from quant_monitor.research import hash_canonical
from quant_monitor.serialization import signal_dict
from quant_monitor.services import MonitoringService
from quant_monitor.storage import SQLiteRepository


def make_signal(metadata=None):
    return Signal(
        strategy="fixture_strategy",
        symbol="600036.SH",
        timestamp=now_market_time(),
        action=SignalAction.BUY,
        strength=0.7,
        price=Decimal("40"),
        reason="offline research fixture",
        metadata=metadata or {},
    )


def make_binding(**changes):
    descriptor = {
        "schema_version": "live-strategy-binding-v1",
        "strategy_name": "fixture_strategy",
        "parameters": {"window": 20},
        "code_identity_status": "unavailable",
        "code_version": None,
    }
    descriptor.update(changes)
    descriptor["binding_hash"] = hash_canonical(descriptor, namespace="live-strategy-binding-v1")
    return descriptor


def test_signal_self_reported_quality_does_not_certify_input_lineage():
    signal = make_signal({"decision_ready": True, "quality": "accepted"})
    result = signal_dict(signal)
    assert result["action"] == "buy"  # Historical model output stays compatible.
    assert result["research_assurance"] == {
        "research_only": True,
        "decision_ready": False,
        "orders_supported": False,
        "input_lineage_status": "unavailable",
        "strategy_binding_status": "missing",
        "notice": "仅为历史研究输出；输入血缘未绑定，不构成交易指令。",
    }


@pytest.mark.parametrize("changes", [
    {},
    {"code_identity_status": "verified_local_source", "code_version": "sha256:" + "1" * 64},
])
def test_consistent_strategy_binding_is_not_data_certification(changes):
    result = signal_dict(make_signal({"strategy_binding": make_binding(**changes)}))
    assurance = result["research_assurance"]
    assert assurance["strategy_binding_status"] == "digest_consistent"
    assert assurance["decision_ready"] is False
    assert assurance["input_lineage_status"] == "unavailable"


@pytest.mark.parametrize("binding", [
    "not-an-object",
    {},
    make_binding(strategy_name="wrong_strategy"),
    make_binding(schema_version="unknown"),
    make_binding(parameters="invalid"),
    make_binding(code_identity_status="verified"),
    make_binding(code_version="wrong"),
    make_binding(code_identity_status="verified_local_source", code_version="sha256:invalid"),
    dict(make_binding(), binding_hash="sha256:" + "0" * 64),
    dict(make_binding(), parameters={"window": 21}),
])
def test_invalid_signal_binding_fails_closed_without_breaking_history(binding):
    result = signal_dict(make_signal({"strategy_binding": binding}))
    assert result["research_assurance"]["strategy_binding_status"] == "invalid"
    assert result["research_assurance"]["decision_ready"] is False


def test_monitor_replaces_strategy_self_reported_binding(tmp_path):
    class UntrustedStrategy:
        name = "fixture_strategy"

    repository = SQLiteRepository(tmp_path / "binding.sqlite3")
    monitor = MonitoringService(
        provider=MockMarketDataProvider(seed=1),
        repository=repository,
        symbols=["600036.SH"],
        collect_bars=False,
    )
    try:
        forged = make_signal({"strategy_binding": make_binding(code_identity_status="verified_local_source")})
        bound = monitor._bind_signal(UntrustedStrategy(), forged)
        actual = bound.metadata["strategy_binding"]
        assert actual["code_identity_status"] == "unavailable"
        assert actual["code_version"] is None
        assert actual["parameters"] == {}
        assert actual != forged.metadata["strategy_binding"]
    finally:
        repository.close()


def test_rest_snapshot_and_event_share_signal_assurance(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from quant_monitor.app import create_app

    settings = Settings(
        data_provider="mock",
        database_path=str(tmp_path / "signals.sqlite3"),
        watchlist=["600036.SH"],
        poll_interval_seconds=60,
        history_bar_count=25,
        bar_interval="1m",
    )
    with TestClient(create_app(settings)) as client:
        runtime = client.app.state.runtime
        signal = make_signal({"strategy_binding": make_binding()})
        runtime.repository.insert_signals([signal])
        payload = client.get("/api/v1/signals", params={"strategy": signal.strategy}).json()
        expected = payload["items"][0]["research_assurance"]
        with client.websocket_connect("/ws/market") as websocket:
            snapshot = websocket.receive_json()
        projected = next(
            item for item in snapshot["data"]["signals"]["items"]
            if item["strategy"] == signal.strategy
        )
        assert projected["research_assurance"] == expected

        messages = []

        async def capture(message):
            messages.append(message)

        runtime.hub.broadcast = capture
        client.portal.call(runtime._on_event, MarketEvent(EventType.SIGNAL, signal))
        assert messages[-1]["data"]["research_assurance"] == expected
        assert messages[-1]["data"]["action"] == "buy"
