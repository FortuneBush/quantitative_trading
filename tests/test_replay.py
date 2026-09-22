"""Positive and negative tests for the offline independent replay gate."""

from datetime import date, datetime, timedelta
from dataclasses import replace
from decimal import Decimal

import pytest

from quant_monitor.backtesting import BacktestConfig, BacktestEngine, BacktestValidationError
from quant_monitor.domain import (
    Bar,
    CollectionBatch,
    CollectionBatchStatus,
    ObservationQualityStatus,
    SHANGHAI_TZ,
    observation_from_bar,
)
from quant_monitor.research import (
    DatasetDescriptor,
    ResearchRunManifest,
    ResearchRunRecord,
    PointInTimeUniverse,
    UniverseMembership,
    hash_canonical,
    hash_input_records,
    strategy_code_version,
    verify_replay,
)
from quant_monitor.serialization import bar_dict, backtest_result_dict
from quant_monitor.strategies import BankDailyMultifactorStrategy


def _evidence(
    *, manifest_parameters=None, manifest_configuration=None, result_parameters=None,
    dataset_attributes=None,
):
    symbols = tuple("600{:03d}.SH".format(index) for index in range(15))
    dates = []
    cursor = datetime(2024, 1, 2, tzinfo=SHANGHAI_TZ)
    while len(dates) < 130:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    bars = {}
    observations = []
    for index, symbol in enumerate(symbols):
        rows = []
        for offset, day in enumerate(dates):
            close = Decimal("10") + Decimal(index) / Decimal("100") + Decimal(offset) / Decimal("1000")
            rows.append(Bar(
                symbol=symbol, interval="1d", timestamp=day.replace(hour=15),
                open=close, high=close + Decimal("0.10"), low=close - Decimal("0.10"),
                close=close, volume=100000 + index, turnover=close * Decimal(100000 + index),
                source="fixture",
            ))
        bars[symbol] = rows
        observations.extend(
            observation_from_bar(
                row, batch_id="replay-batch", received_at=day.replace(hour=16),
                persisted_at=day.replace(hour=16, minute=1),
                quality_status=ObservationQualityStatus.ACCEPTED,
            )
            for row, day in zip(rows, dates)
        )
    input_hash = hash_input_records(bar_dict(row) for rows in bars.values() for row in rows)
    batch = CollectionBatch(
        batch_id="replay-batch", provider="fixture", request_kind="bars",
        input_hash="0" * 64, status=CollectionBatchStatus.SUCCEEDED,
        started_at=dates[0].replace(hour=16), completed_at=dates[-1].replace(hour=16),
        item_count=len(observations),
    )
    manifest = ResearchRunManifest(
        run_id="replay-positive", strategy_name="bank_daily_multifactor_v1",
        strategy_parameters=manifest_parameters or {}, strategy_code_version=strategy_code_version("bank_daily_multifactor_v1"),
        dataset=DatasetDescriptor(
            name="fixture-daily", schema_version="bars-v1", source="fixture",
            record_count=len(observations), attributes=(
                {"batch_id": "replay-batch"}
                if dataset_attributes is None else dataset_attributes
            ),
        ), input_hash=input_hash, calendar_version="weekday-fallback-v1",
        universe_version="fixture-v1", adjustment_version="raw-v1",
        constraint_version="fixture-v1", configuration=manifest_configuration or {}, assumptions={},
    )
    result = BacktestEngine(
        BankDailyMultifactorStrategy(**(result_parameters or {})), BacktestConfig()
    ).run(bars)
    payload = backtest_result_dict(result)
    record = ResearchRunRecord.from_manifest(manifest, created_at=dates[-1].replace(hour=17))
    return record, manifest.to_dict(), payload, observations, batch


def test_replay_does_not_attest_declared_pit_universe_without_evidence():
    """A manifest snapshot claim must not be treated as a display-only label."""

    record, manifest, payload, observations, batch = _evidence(
        dataset_attributes={
            "batch_id": "replay-batch",
            "universe_snapshot_hashes": ["sha256:" + "a" * 64],
        }
    )
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert report.checks["manifest_integrity"] is True
    assert report.checks["universe_binding_matches"] is False
    assert "universe_binding_matches" in report.failures


def test_replay_accepts_persisted_snapshot_mapping_and_binds_each_session():
    """A repository-shaped snapshot mapping must reproduce the PIT result."""

    record, base_manifest, _payload, observations, batch = _evidence()
    symbols = sorted({item.symbol for item in observations})
    memberships = tuple(
        UniverseMembership(
            universe="fixture",
            symbol=symbol,
            effective_from=date(2023, 1, 1),
            effective_to=None,
            known_at=datetime(2023, 1, 1, 8, tzinfo=SHANGHAI_TZ),
            active=True,
            exchange=symbol.rsplit(".", 1)[1],
            security_type="stock",
            industry="fixture",
            source="fixture",
            version="fixture-v1",
            revision_id="r1",
        )
        for symbol in symbols
    )
    resolver = PointInTimeUniverse(memberships, version="fixture-v1")
    cutoff = datetime(2023, 1, 1, 8, tzinfo=SHANGHAI_TZ)
    sessions = sorted({item.session_date for item in observations})
    snapshots = {session: resolver.snapshot_at(session, cutoff) for session in sessions}

    bars = {}
    for item in observations:
        bars.setdefault(item.symbol, []).append(item.to_legacy_bar())
    pit_result = BacktestEngine(
        BankDailyMultifactorStrategy(), BacktestConfig()
    ).run(bars, point_in_time_universe=snapshots)
    pit_payload = backtest_result_dict(pit_result)

    manifest = ResearchRunManifest(
        run_id="replay-persisted-pit",
        strategy_name=base_manifest["strategy"]["name"],
        strategy_parameters=base_manifest["strategy"]["parameters"],
        strategy_code_version=base_manifest["strategy"]["code_version"],
        dataset=DatasetDescriptor(
            name=base_manifest["dataset"]["name"],
            schema_version=base_manifest["dataset"]["schema_version"],
            source=base_manifest["dataset"]["source"],
            record_count=base_manifest["dataset"]["record_count"],
            attributes={
                "batch_id": "replay-batch",
                "universe_snapshot_hashes": sorted(
                    item.content_hash for item in snapshots.values()
                ),
            },
        ),
        input_hash=base_manifest["input_hash"],
        calendar_version=base_manifest["versions"]["calendar"],
        universe_version="fixture-v1",
        adjustment_version=base_manifest["versions"]["adjustment"],
        constraint_version=base_manifest["versions"]["constraints"],
        configuration=base_manifest["configuration"],
        assumptions=base_manifest["assumptions"],
    )
    record = ResearchRunRecord.from_manifest(
        manifest, created_at=datetime(2025, 1, 2, 17, tzinfo=SHANGHAI_TZ)
    )
    report = verify_replay(
        record,
        manifest.to_dict(),
        pit_payload,
        observations,
        [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
        point_in_time_universe=snapshots,
        require_universe_binding=True,
    )
    assert report.verified is True
    assert report.checks["universe_binding_matches"] is True


def test_replay_rejects_persisted_snapshot_mapping_with_future_cutoff():
    """A persisted row learned after its session cannot enter replay."""

    _record, _manifest, _payload, observations, _batch = _evidence()
    symbols = sorted({item.symbol for item in observations})
    resolver = PointInTimeUniverse(
        [
            UniverseMembership(
                universe="fixture",
                symbol=symbol,
                effective_from=date(2023, 1, 1),
                effective_to=None,
                known_at=datetime(2023, 1, 1, 8, tzinfo=SHANGHAI_TZ),
                active=True,
                exchange=symbol.rsplit(".", 1)[1],
                security_type="stock",
                industry="fixture",
                source="fixture",
                version="fixture-v1",
                revision_id="r1",
            )
            for symbol in symbols
        ],
        version="fixture-v1",
    )
    session = min(item.session_date for item in observations)
    snapshots = {
        session: resolver.snapshot_at(
            session,
            datetime(2030, 1, 1, 8, tzinfo=SHANGHAI_TZ),
        )
    }
    bars = {item.symbol: [item.to_legacy_bar()] for item in observations if item.session_date == session}
    with pytest.raises(BacktestValidationError, match="knowledge_cutoff"):
        BacktestEngine(BankDailyMultifactorStrategy(), BacktestConfig()).run(
            bars, point_in_time_universe=snapshots
        )


def test_backtest_rejects_persisted_snapshot_mapping_missing_session():
    """A partial persisted mapping cannot be applied to a longer bar set."""

    _record, _manifest, _payload, observations, _batch = _evidence()
    symbols = sorted({item.symbol for item in observations})
    resolver = PointInTimeUniverse(
        [
            UniverseMembership(
                universe="fixture",
                symbol=symbol,
                effective_from=date(2023, 1, 1),
                effective_to=None,
                known_at=datetime(2023, 1, 1, 8, tzinfo=SHANGHAI_TZ),
                active=True,
                exchange=symbol.rsplit(".", 1)[1],
                security_type="stock",
                industry="fixture",
                source="fixture",
                version="fixture-v1",
                revision_id="r1",
            )
            for symbol in symbols
        ],
        version="fixture-v1",
    )
    sessions = sorted({item.session_date for item in observations})
    snapshots = {
        sessions[0]: resolver.snapshot_at(
            sessions[0], datetime(2023, 1, 1, 8, tzinfo=SHANGHAI_TZ)
        )
    }
    bars = {}
    for item in observations:
        bars.setdefault(item.symbol, []).append(item.to_legacy_bar())
    with pytest.raises(BacktestValidationError, match="cover every observed session"):
        BacktestEngine(BankDailyMultifactorStrategy(), BacktestConfig()).run(
            bars, point_in_time_universe=snapshots
        )


def test_independent_replay_matches_an_immutable_bank_artifact():
    record, manifest, payload, observations, batch = _evidence()
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is True
    assert report.replay_verified is True
    assert report.failures == ()
    assert report.to_dict()["result_hash_matches"] is True


def test_strict_replay_requires_calendar_evidence_in_collection_batch():
    """A manifest calendar label alone must not authenticate strict replay."""

    record, manifest, payload, observations, batch = _evidence()
    report = verify_replay(
        record,
        manifest,
        payload,
        observations,
        [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
        require_calendar_binding=True,
    )
    assert report.verified is False
    assert report.checks["calendar_binding_matches"] is False
    assert "calendar_binding_matches" in report.failures


def test_strict_replay_accepts_matching_calendar_evidence():
    """Strict replay passes the calendar gate only with a matching batch fact."""

    record, manifest, payload, observations, batch = _evidence()
    batch = replace(
        batch,
        metadata={"calendar": {"version": "weekday-fallback-v1"}},
    )
    report = verify_replay(
        record,
        manifest,
        payload,
        observations,
        [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
        require_calendar_binding=True,
    )
    assert report.verified is True
    assert report.checks["calendar_binding_matches"] is True


def test_replay_rejects_human_code_version_label():
    """A display label must not masquerade as an executable identity."""
    record, manifest, payload, observations, batch = _evidence()
    manifest["strategy"]["code_version"] = "bank_daily_multifactor_v1"
    # The content hash is intentionally now stale; this assertion specifically
    # ensures the dedicated implementation check is present in the report.
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert report.checks["strategy_code_version_matches"] is False
    assert "strategy_code_version_matches" in report.failures
    assert report.replayed_result_hash is None


def test_replay_rejects_strategy_source_drift(monkeypatch):
    """A changed local implementation invalidates an otherwise valid run."""
    record, manifest, payload, observations, batch = _evidence()
    monkeypatch.setattr(
        "quant_monitor.research.replay.strategy_code_version",
        lambda name: "sha256:" + "0" * 64,
    )
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert report.checks["manifest_integrity"] is True
    assert report.checks["strategy_code_version_matches"] is False
    assert report.replayed_result_hash is None


def test_replay_error_exposes_only_safe_exception_type(monkeypatch):
    """Governance payloads must not echo malformed input or response text."""
    record, manifest, payload, observations, batch = _evidence()

    def explode(_name):
        raise RuntimeError("secret-token https://example.test/private")

    monkeypatch.setattr("quant_monitor.research.replay.strategy_code_version", explode)
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert report.error == "RuntimeError"
    assert "secret-token" not in report.to_dict()["error"]


def test_independent_replay_rejects_a_hash_consistent_but_changed_result():
    record, manifest, payload, observations, batch = _evidence()
    payload["metrics"]["total_return"] = 0.5
    payload.pop("result_hash")
    payload["result_hash"] = hash_canonical(payload, namespace="backtest-result-v1")
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert "replay_result_hash_matches" in report.failures


def test_replay_rejects_override_that_detaches_execution_from_the_manifest():
    # The payload was produced with defaults, but the immutable manifest says
    # min_universe=16. Before binding enforcement, overriding with {} attested
    # this deliberately detached payload because its replay hash still matched.
    record, manifest, payload, observations, batch = _evidence(
        manifest_parameters={"min_universe": 16},
    )
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ), strategy_config={},
    )
    assert report.verified is False
    assert report.checks["manifest_integrity"] is True
    assert report.checks["result_integrity"] is True
    assert "strategy_configuration_bound" in report.failures
    assert report.replayed_result_hash is None


@pytest.mark.parametrize("parameters", [
    {"minimum_universe": 15},
    {"parameters": {"unknown_parameter": 1}},
    {"min_universe": 15, "parameters": {"min_universe": 16}},
])
def test_replay_rejects_unknown_or_ambiguous_manifest_strategy_parameters(parameters):
    record, manifest, payload, observations, batch = _evidence(manifest_parameters=parameters)
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert "strategy_configuration_bound" in report.failures
    assert report.replayed_result_hash is None


def test_replay_rejects_unknown_execution_configuration_instead_of_using_defaults():
    record, manifest, payload, observations, batch = _evidence(
        manifest_configuration={"initial_cah": "250000"},
    )
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert "execution_configuration_supported" in report.failures
    assert report.replayed_result_hash is None


@pytest.mark.parametrize("wrapped", [False, True])
def test_replay_retains_bound_nondefault_parameters_and_unambiguous_legacy_wrapper(wrapped):
    parameters = {"min_universe": 16}
    declared = {"parameters": parameters} if wrapped else parameters
    record, manifest, payload, observations, batch = _evidence(
        manifest_parameters=declared, result_parameters=parameters,
    )
    report = verify_replay(
        record, manifest, payload, observations, [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ), strategy_config=parameters,
    )
    assert report.verified is True
    assert report.checks["strategy_configuration_bound"] is True
    assert report.checks["execution_configuration_supported"] is True


def test_replay_rejects_batch_provider_calendar_or_response_receipt_rebinding():
    """Persisted batch metadata must remain aligned with the run manifest."""
    record, manifest, payload, observations, batch = _evidence()
    batch = replace(
        batch,
        provider="other-provider",
        metadata={
            "calendar": {"version": "different-calendar"},
            "response_content_hash": "0" * 64,
            "response_content_hash_scope": "parsed_json_response",
        },
    )
    report = verify_replay(
        record,
        manifest,
        payload,
        observations,
        [batch],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert "batch_provider_matches" in report.failures
    assert "calendar_binding_matches" in report.failures

    malformed = replace(
        batch,
        provider="fixture",
        metadata={
            "response_content_hash": "not-a-digest",
            "response_content_hash_scope": "raw_http_body",
        },
    )
    report = verify_replay(
        record, manifest, payload, observations, [malformed],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.checks["batch_provider_matches"] is True
    assert report.checks["response_receipt_consistent"] is False


def test_replay_rejects_batch_item_count_rebinding():
    """A forged terminal count must not detach replay from its observations."""
    record, manifest, payload, observations, batch = _evidence()
    rebound = replace(batch, item_count=batch.item_count + 1)
    report = verify_replay(
        record, manifest, payload, observations, [rebound],
        datetime(2025, 1, 2, 9, tzinfo=SHANGHAI_TZ),
    )
    assert report.verified is False
    assert report.checks["successful_collection_batches"] is True
    assert report.checks["batch_item_count_matches"] is False
    assert "batch_item_count_matches" in report.failures
    assert report.replayed_result_hash is None
