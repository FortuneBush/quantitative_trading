from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.domain import (
    Bar,
    CollectionBatch,
    CollectionBatchStatus,
    ObservationQualityStatus,
    observation_from_bar,
    SHANGHAI_TZ,
)
from quant_monitor.research import (
    LineageGateReport,
    audit_observation_lineage,
    hash_input_records,
)
from quant_monitor.trading_calendar import VersionedSessionCalendar
from quant_monitor.serialization import bar_dict


BASE = datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI_TZ)


def _observation(*, quality=ObservationQualityStatus.ACCEPTED):
    bar = Bar(
        symbol="600000.SH",
        interval="1d",
        timestamp=BASE,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=100,
        turnover=Decimal("1050"),
        source="asharehub",
    )
    return observation_from_bar(
        bar,
        batch_id="batch-1",
        received_at=datetime(2024, 1, 3, 9, 0, tzinfo=SHANGHAI_TZ),
        persisted_at=datetime(2024, 1, 3, 9, 0, 1, tzinfo=SHANGHAI_TZ),
        quality_status=quality,
    )


def _batch():
    return CollectionBatch(
        batch_id="batch-1",
        provider="asharehub",
        request_kind="bars",
        input_hash="0" * 64,
        status=CollectionBatchStatus.SUCCEEDED,
        started_at=datetime(2024, 1, 3, 8, 59, tzinfo=SHANGHAI_TZ),
        completed_at=datetime(2024, 1, 3, 9, 0, 1, tzinfo=SHANGHAI_TZ),
        item_count=1,
    )


def test_lineage_gate_passes_only_with_complete_evidence():
    item = _observation()
    expected_hash = hash_input_records([bar_dict(item.to_legacy_bar())])
    report = audit_observation_lineage(
        [item],
        expected_input_hash=expected_hash,
        expected_record_count=1,
        expected_source="asharehub",
        decision_at=datetime(2024, 1, 4, 9, 0, tzinfo=SHANGHAI_TZ),
        batches=[_batch()],
    )
    assert report.verified is True
    assert report.failures == ()


def test_lineage_gate_is_fail_closed_for_degraded_or_missing_context():
    item = _observation(quality=ObservationQualityStatus.DEGRADED)
    report = audit_observation_lineage(
        [item], expected_source="asharehub", expected_record_count=1
    )
    assert report.verified is False
    assert "accepted_quality" in report.failures
    assert "availability_attested" in report.failures
    assert "input_hash_matches" in report.failures
    assert "successful_collection_batches" in report.failures


def test_lineage_gate_rejects_weekend_observation_even_with_other_evidence():
    """A weekend row cannot become decision evidence under the fallback calendar."""
    weekend_bar = Bar(
        symbol="600000.SH", interval="1d",
        timestamp=BASE + timedelta(days=4),
        open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
        close=Decimal("10.5"), volume=100, turnover=Decimal("1050"),
        source="asharehub",
    )
    item = observation_from_bar(
        weekend_bar, batch_id="batch-1",
        received_at=datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI_TZ),
        persisted_at=datetime(2024, 1, 8, 9, 0, 1, tzinfo=SHANGHAI_TZ),
    )
    expected_hash = hash_input_records([bar_dict(item.to_legacy_bar())])
    report = audit_observation_lineage(
        [item], expected_input_hash=expected_hash, expected_record_count=1,
        expected_source="asharehub",
        decision_at=datetime(2024, 1, 9, 9, 0, tzinfo=SHANGHAI_TZ),
        batches=[_batch()],
    )
    assert report.verified is False
    assert "session_dates_valid" in report.failures


def test_lineage_gate_honors_injected_finite_calendar():
    calendar = VersionedSessionCalendar(
        (BASE.date() + timedelta(days=1),), source="fixture", version="v1"
    )
    report = audit_observation_lineage(
        [_observation()], expected_record_count=1, expected_source="asharehub",
        decision_at=datetime(2024, 1, 4, 9, 0, tzinfo=SHANGHAI_TZ),
        batches=[_batch()], calendar=calendar,
    )
    assert report.verified is False
    assert "session_dates_valid" in report.failures


def test_lineage_gate_rejects_batch_item_count_rebinding():
    """A successful batch must account for exactly the rows being audited."""
    item = _observation()
    expected_hash = hash_input_records([bar_dict(item.to_legacy_bar())])
    rebound = CollectionBatch(
        batch_id="batch-1", provider="asharehub", request_kind="bars",
        input_hash="0" * 64, status=CollectionBatchStatus.SUCCEEDED,
        started_at=datetime(2024, 1, 3, 8, 59, tzinfo=SHANGHAI_TZ),
        completed_at=datetime(2024, 1, 3, 9, 0, 1, tzinfo=SHANGHAI_TZ),
        item_count=2,
    )
    report = audit_observation_lineage(
        [item], expected_input_hash=expected_hash, expected_record_count=1,
        expected_source="asharehub",
        decision_at=datetime(2024, 1, 4, 9, 0, tzinfo=SHANGHAI_TZ),
        batches=[rebound],
    )
    assert report.verified is False
    assert "batch_item_count_matches" in report.failures


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_lineage_report_rejects_truthy_non_boolean_checks(value):
    with pytest.raises(TypeError, match="lineage checks must be booleans"):
        LineageGateReport({"evidence": value})
