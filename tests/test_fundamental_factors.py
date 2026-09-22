from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import FundamentalObservation
from quant_monitor.research import (
    BankFundamentalFactorReport,
    build_bank_fundamental_factors,
)


DECISION_AT = datetime(2026, 9, 18, 15, 30, tzinfo=timezone.utc)
METRICS = {
    "pb": ("ratio", Decimal("0.8")),
    "roe": ("percent", Decimal("12")),
    "dividend_yield": ("percent", Decimal("4")),
    "npl_ratio": ("percent", Decimal("1.0")),
    "provision_coverage": ("percent", Decimal("220")),
    "capital_adequacy": ("percent", Decimal("13")),
    "cost_income_ratio": ("percent", Decimal("30")),
}


def _fact(symbol, metric, value, *, unit=None, available_at=DECISION_AT, source="fixture"):
    selected_unit, default_value = METRICS[metric]
    return FundamentalObservation(
        observation_id="{}-{}-{}".format(symbol, metric, available_at.date()),
        symbol=symbol,
        metric=metric,
        value=default_value if value is None else Decimal(str(value)),
        unit=unit or selected_unit,
        scope="consolidated",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        available_at=available_at,
        received_at=available_at,
        source=source,
        revision_id="r1",
        metadata={},
    )


def _complete(symbol, multiplier=1):
    return [_fact(symbol, metric, value * multiplier) for metric, (_unit, value) in METRICS.items()]


def test_bank_factors_require_complete_point_in_time_metric_coverage():
    report = build_bank_fundamental_factors(
        _complete("600036.SH") + _complete("601166.SH", multiplier=Decimal("1.1")),
        decision_at=DECISION_AT,
    )
    assert report.quality_status == "ok"
    assert report.complete_count == 2
    assert all(item.score_status == "complete" for item in report.records)
    assert report.records[0].score is not None
    assert report.records[0].values["pb"] == Decimal("0.8")
    assert report.records[1].score > report.records[0].score


def test_bank_factors_do_not_call_one_symbol_a_cross_section():
    report = build_bank_fundamental_factors(
        _complete("600036.SH"),
        decision_at=DECISION_AT,
    )
    assert report.records[0].score is None
    assert report.records[0].score_status == "unavailable"
    assert "fundamental_cross_section_too_small" in report.warnings


def test_bank_factors_keep_missing_members_and_do_not_fill_score_with_zero():
    observations = _complete("600036.SH")
    observations = [item for item in observations if item.metric != "roe"]
    report = build_bank_fundamental_factors(
        observations,
        decision_at=DECISION_AT,
        symbols=["600036.SH", "601166.SH"],
    )
    assert report.quality_status == "partial"
    first, second = report.records
    assert first.score is None
    assert first.score_status == "unavailable"
    assert first.missing_metrics == ("roe",)
    assert second.missing_metrics == tuple(item.name for item in report.metric_specs)
    assert second.score is None
    assert report.complete_count == 0
    assert "fundamental_metric_coverage_incomplete" in report.warnings


def test_bank_factors_exclude_future_facts_and_reject_unit_mismatch():
    observations = _complete("600036.SH")
    observations.append(
        _fact(
            "600036.SH",
            "roe",
            Decimal("99"),
            available_at=datetime(2026, 9, 19, 15, 30, tzinfo=timezone.utc),
        )
    )
    observations = [
        item if item.metric != "pb" else _fact("600036.SH", "pb", item.value, unit="percent")
        for item in observations
        if not (item.metric == "roe" and item.available_at.date() == date(2026, 9, 18))
    ]
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    item = report.records[0]
    assert item.score is None
    assert item.invalid_unit_metrics == ("pb",)
    assert "roe" in item.missing_metrics


def test_bank_factors_do_not_mix_sources_without_an_explicit_policy():
    observations = _complete("600036.SH") + _complete("601166.SH", multiplier=Decimal("1.1"))
    observations[-1] = _fact("601166.SH", "cost_income_ratio", Decimal("33"), source="other")
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    assert report.complete_count == 0
    assert all(item.score is None for item in report.records)
    assert "fundamental_sources_mixed" in report.warnings
    assert all(item.source_conflict for item in report.records)


@pytest.mark.parametrize("scope", ["standalone", "unknown"])
def test_bank_factors_reject_mixed_or_unknown_scopes_but_preserve_evidence(scope):
    observations = _complete("600036.SH") + [
        replace(item, scope=scope) for item in _complete("601166.SH")
    ]
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    assert report.quality_status == "partial"
    assert report.complete_count == 0
    assert all(item.score is None and item.scope_conflict for item in report.records)
    assert report.records[1].evidence["roe"]["scope"] == scope
    assert report.records[1].values["roe"] == Decimal("12")
    assert "fundamental_scopes_incomparable" in report.warnings
    assert bool(report.records[1].invalid_scope_metrics) == (scope == "unknown")


def test_bank_factors_do_not_score_uniform_unknown_scope():
    observations = [
        replace(item, scope="unknown")
        for item in _complete("600036.SH") + _complete("601166.SH")
    ]
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    assert report.complete_count == 0
    assert all(item.score is None for item in report.records)
    assert "fundamental_scopes_incomparable" in report.warnings


def test_bank_factors_require_equal_period_bounds_per_metric():
    observations = _complete("600036.SH") + _complete("601166.SH")
    observations[-1] = replace(observations[-1], period_start=date(2025, 7, 1))
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    assert report.complete_count == 0
    assert all(item.incomparable_period_metrics == ("cost_income_ratio",) for item in report.records)
    assert "fundamental_periods_incomparable" in report.warnings
    assert report.records[1].evidence["cost_income_ratio"]["period_start"] == "2025-07-01"


def test_bank_factors_allow_metric_specific_but_cross_section_comparable_periods():
    observations = [
        replace(item, period_start=date(2026, 9, 18), period_end=date(2026, 9, 18))
        if item.metric == "pb" else item
        for item in _complete("600036.SH") + _complete("601166.SH")
    ]
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    assert report.complete_count == 2
    assert report.quality_status == "ok"


def test_bank_factors_scope_selection_precedes_unrelated_ambiguity_resolution():
    observations = _complete("600036.SH") + _complete("601166.SH")
    unrelated = replace(observations[0], observation_id="unrelated-a", symbol="600519.SH")
    unrelated_conflict = replace(unrelated, observation_id="unrelated-b", scope="standalone")
    other_metric = replace(observations[0], observation_id="other-a", metric="non_bank_metric")
    other_conflict = replace(other_metric, observation_id="other-b", scope="standalone")
    report = build_bank_fundamental_factors(
        observations + [unrelated, unrelated_conflict, other_metric, other_conflict],
        decision_at=DECISION_AT, symbols=["600036.SH", "601166.SH"],
    )
    assert report.complete_count == 2
    assert [item.symbol for item in report.records] == ["600036.SH", "601166.SH"]


def test_bank_factors_keep_explicit_empty_universe_empty():
    report = build_bank_fundamental_factors(
        _complete("600036.SH") + _complete("601166.SH"),
        decision_at=DECISION_AT, symbols=[],
    )
    assert report.quality_status == "empty"
    assert report.records == ()


def test_bank_factors_evidence_is_detached_immutable_and_excludes_provider_metadata():
    observations = _complete("600036.SH") + _complete("601166.SH")
    observations[0] = replace(observations[0], metadata={"private_provider_field": "do-not-publish"})
    report = build_bank_fundamental_factors(observations, decision_at=DECISION_AT)
    record = report.records[0]
    evidence = record.evidence["pb"]
    assert evidence["observation_id"] == observations[0].observation_id
    assert evidence["content_hash"] == observations[0].content_hash
    assert set(evidence) == {
        "observation_id", "content_hash", "period_start", "period_end", "available_at",
        "received_at", "unit", "scope", "source", "revision_id",
    }
    with pytest.raises(TypeError):
        evidence["scope"] = "unknown"
    serialized = report.to_dict()
    serialized["items"][0]["evidence"]["pb"]["scope"] = "unknown"
    assert evidence["scope"] == "consolidated"
    assert "private_provider_field" not in str(report.to_dict())


def test_bank_factors_retain_wrong_unit_and_explicit_null_fact_evidence():
    observations = _complete("600036.SH") + _complete("601166.SH")
    observations[0] = replace(observations[0], unit="percent")
    observations[1] = replace(observations[1], value=None)
    record = build_bank_fundamental_factors(observations, decision_at=DECISION_AT).records[0]
    assert record.values["pb"] == Decimal("0.8")
    assert record.evidence["pb"]["unit"] == "percent"
    assert record.invalid_unit_metrics == ("pb",)
    assert record.missing_metrics == ("roe",)
    assert record.evidence["roe"]["content_hash"] == observations[1].content_hash
    assert record.revision_ids["roe"] == "r1"


def test_bank_factor_constructor_rejects_forged_complete_state_and_evidence():
    report = build_bank_fundamental_factors(
        _complete("600036.SH") + _complete("601166.SH"), decision_at=DECISION_AT,
    )
    record = report.records[0]
    assert replace(record, score_status=" COMPLETE ").score_status == "complete"
    with pytest.raises(ValueError, match="missing metrics"):
        replace(record, values={})
    with pytest.raises(ValueError, match="provenance evidence"):
        replace(record, evidence={})
    with pytest.raises(ValueError, match="content hash"):
        replace(record, values={**record.values, "pb": Decimal("99")})
    with pytest.raises(ValueError, match="revisions"):
        replace(record, revision_ids={})
    with pytest.raises(ValueError, match="mixed sources or scopes"):
        replace(record, source_conflict=True, score_status=" COMPLETE ")
    changed_evidence = {metric: dict(item) for metric, item in record.evidence.items()}
    changed_evidence["pb"]["available_at"] = (DECISION_AT + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="availability"):
        replace(record, evidence=changed_evidence)


def test_bank_factor_report_rejects_wrong_times_incomplete_cohorts_and_forged_scores():
    report = build_bank_fundamental_factors(
        _complete("600036.SH") + _complete("601166.SH"), decision_at=DECISION_AT,
    )
    with pytest.raises(ValueError, match="decision time"):
        replace(report, decision_at=DECISION_AT + timedelta(seconds=1))
    with pytest.raises(ValueError, match="at least two"):
        replace(report, records=report.records[:1])
    with pytest.raises(ValueError, match="complete cross-section"):
        replace(report, records=(report.records[0], replace(report.records[1], score_status="unavailable", score=None)))
    with pytest.raises(ValueError, match="cross-sectional values"):
        replace(report, records=(replace(report.records[0], score=0.9), report.records[1]))
    with pytest.raises(ValueError, match="fixed metric"):
        BankFundamentalFactorReport(DECISION_AT, report.records, metric_specs=())


def test_bank_factor_api_is_cache_only_and_keeps_empty_state_explicit(tmp_path):
    settings = Settings(
        app_name="factor API fixture",
        database_path=str(tmp_path / "factor-api.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=10,
        watchlist=["600036.SH", "601166.SH"],
        mock_seed=20260918,
    )
    with TestClient(create_app(settings)) as client:
        empty = client.get(
            "/api/v1/fundamentals/factors/as-of?as_of=2026-09-18T15:30:00%2B08:00"
        )
        assert empty.status_code == 200
        assert empty.json()["quality"]["status"] == "empty"
        assert empty.json()["provider_operations"] == 0

        client.app.state.runtime.repository.insert_fundamental_observations(
            _complete("600036.SH") + _complete("601166.SH", multiplier=Decimal("1.1"))
        )
        response = client.get(
            "/api/v1/fundamentals/factors/as-of?as_of=2026-09-18T23:30:00%2B08:00"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["served_from"] == "sqlite_cache"
        assert body["upstream_requests"] == 0
        assert body["count"] == 2
        assert body["complete_count"] == 2
        assert body["quality"]["status"] == "ok"
        assert all(item["score_status"] == "complete" for item in body["items"])
