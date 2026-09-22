"""Public lineage metadata is not a channel for provider credentials or paths."""

from dataclasses import dataclass
from decimal import Decimal
import json

import pytest

from quant_monitor.domain import (
    Alert, AlertLevel, CollectionBatch, CollectionBatchStatus, Signal, SignalAction,
    parse_market_time,
)
from quant_monitor.serialization import alert_dict, collection_batch_dict, jsonable, signal_dict


def _serialize_metadata(metadata):
    batch = CollectionBatch(
        batch_id="privacy-fixture",
        provider="mock",
        request_kind="bars",
        input_hash="a" * 64,
        status=CollectionBatchStatus.RUNNING,
        started_at=parse_market_time("2026-09-01T10:00:00+08:00"),
        metadata=metadata,
    )
    return collection_batch_dict(batch)["metadata"]


@pytest.mark.parametrize(
    "sensitive",
    [
        "ash_" + "x" * 48,
        "https://vendor.example/data?token=private-value",
        "https%3A%2F%2Fvendor.example%2Fdata%3Fapi_key%3Dprivate-value",
        "Bearer private-value-123",
        '{"nested":{"client_secret":"private-value"}}',
        "C:\\Users\\private-user\\data.json",
        "/home/private-user/data.json",
    ],
)
def test_lineage_redacts_sensitive_values_under_innocent_keys(sensitive):
    metadata = _serialize_metadata({"note": sensitive, "nested": [{"message": sensitive}]})
    assert metadata == {"note": "[redacted]", "nested": [{"message": "[redacted]"}]}
    assert sensitive not in json.dumps(metadata)


def test_lineage_does_not_expose_secrets_used_as_mapping_keys():
    secret = "ash_" + "x" * 48
    metadata = _serialize_metadata({secret: None, "C:\\private\\file": 1, "count": 2})
    assert metadata == {"count": 2}


def test_lineage_sensitive_key_policy_handles_unicode_and_benign_token_counts():
    metadata = _serialize_metadata({
        "apiKey": "private-value",
        "API密钥": "private-value",
        "access\u200bToken": "private-value",
        "token_count": 3,
        "request": {"symbol": "600519.SH", "interval": "1d"},
        "response_content_hash": "a" * 64,
        "ratio": Decimal("0.75"),
    })
    assert metadata["apiKey"] == "[redacted]"
    assert metadata["API密钥"] == "[redacted]"
    assert metadata["access\u200bToken"] == "[redacted]"
    assert metadata["token_count"] == 3
    assert metadata["request"] == {"symbol": "600519.SH", "interval": "1d"}
    assert metadata["response_content_hash"] == "a" * 64
    assert metadata["ratio"] == "0.75"
    assert "private-value" not in json.dumps(metadata)


def test_lineage_scans_typed_values_and_does_not_serialize_opaque_objects():
    @dataclass
    class ProviderReceipt:
        note: str

    metadata = _serialize_metadata({
        "receipt": ProviderReceipt("https://vendor.example/data?token=private-value"),
        "opaque": object(),
    })
    assert metadata == {"receipt": "[redacted]", "opaque": "[redacted]"}


def test_public_generic_exception_serialization_omits_untrusted_message():
    result = jsonable({"nested": ValueError("Authorization: private-value")})
    assert result == {"nested": {"error_type": "ValueError"}}


def test_alert_and_signal_free_text_cannot_expose_credentials_or_paths():
    secret = "ash_" + "x" * 48
    at = parse_market_time("2026-09-01T10:00:00+08:00")
    alert = Alert(
        symbol=None, timestamp=at, level=AlertLevel.WARNING,
        title=secret, message="provider failed: " + secret,
        category="C:\\private\\data.json",
    )
    payload = alert_dict(alert)
    assert payload["title"] == payload["message"] == payload["category"] == "[redacted]"
    signal = Signal(
        strategy=secret, symbol="600519.SH", timestamp=at,
        action=SignalAction.HOLD, strength=0.0, reason="token=private-value",
    )
    payload = signal_dict(signal)
    assert payload["strategy"] == payload["strategy_name"] == payload["reason"] == "[redacted]"
    assert secret not in json.dumps(payload)


def test_lineage_recursive_metadata_remains_bounded():
    circular = {}
    circular["nested"] = circular
    metadata = _serialize_metadata(circular)
    for _ in range(9):
        metadata = metadata["nested"]
    assert metadata == "[truncated]"


def test_persisted_lineage_metadata_is_redacted_at_the_http_boundary(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from quant_monitor.app import create_app
    from quant_monitor.config import Settings

    settings = Settings(
        data_provider="mock", database_path=str(tmp_path / "privacy.sqlite3"),
        poll_interval_seconds=60, history_bar_count=10, watchlist=["600519.SH"],
    )
    with TestClient(create_app(settings)) as client:
        secret = "ash_" + "x" * 48
        batch = CollectionBatch(
            batch_id="persisted-privacy-fixture", provider="mock", request_kind="bars",
            input_hash="a" * 64, status=CollectionBatchStatus.RUNNING,
            started_at=parse_market_time("2026-09-01T10:00:00+08:00"),
            metadata={"debug": secret, secret: "discard", "request_count": 1},
        )
        # Older data or future adapters may have stored an unsafe value;
        # sanitizing only during collection does not protect existing rows.
        client.app.state.runtime.repository.begin_collection_batch(batch)
        response = client.get("/api/v1/lineage/batches/" + batch.batch_id)
        assert response.status_code == 200, response.text
        assert secret not in response.text
        assert response.json()["metadata"] == {"debug": "[redacted]", "request_count": 1}
