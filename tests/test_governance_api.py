"""Cross-layer checks: valid artifact hashes cannot authenticate claims."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from quant_monitor.backtesting import BacktestEngine, audit_backtest_payload_metrics
from quant_monitor.domain import Bar
from quant_monitor.research import (
    UniverseMembership,
    UniverseSnapshot,
    hash_canonical,
)
from quant_monitor.serialization import backtest_result_dict
from quant_monitor.strategies import Strategy
from tests.test_research_lineage_api import lineage_client, _persist_evidence


class _FixtureStrategy(Strategy):
    @property
    def name(self):
        return "fixture"

    def evaluate(self, context):
        return ()


def _store_claim(client, *, bind_batch=True, mutate=None):
    repo = client.app.state.runtime.repository
    manifest, observation, batch, cutoff = _persist_evidence(repo, bind_batch=bind_batch)
    bar = Bar(
        symbol=observation.symbol, interval="1d", timestamp=observation.period_end,
        open=observation.open, high=observation.high, low=observation.low,
        close=observation.close, volume=observation.volume,
        turnover=observation.turnover, source=observation.source,
    )
    payload = backtest_result_dict(BacktestEngine(_FixtureStrategy()).run({bar.symbol: [bar]}))
    payload["diagnostics"]["decision_grade"] = True
    payload["diagnostics"]["decision_grade_checks"] = {"self_declared": True}
    payload["diagnostics"]["decision_grade_failures"] = []
    payload["diagnostics"]["warnings"] = []
    # Both the JSON hash and all numeric metrics are genuinely consistent.
    # That still cannot turn caller input into provenance/replay evidence.
    assert audit_backtest_payload_metrics(payload).verified
    if mutate:
        mutate(payload)
    payload.pop("result_hash")
    payload["result_hash"] = hash_canonical(payload, namespace="backtest-result-v1")
    response = client.post(
        "/api/v1/research/runs/{}/result".format(manifest.run_id), json={"result": payload}
    )
    assert response.status_code == 201, response.text
    return manifest, cutoff, repo.get_research_result(manifest.run_id).result_json


def _pit_manifest(repository, *, recorded_at, snapshot, run_id):
    """Clone the fixture manifest with an explicit v9 snapshot declaration."""

    manifest, _, _, _ = _persist_evidence(repository)
    dataset = replace(
        manifest.dataset,
        attributes={
            "batch_id": "lineage-api-batch",
            "universe_snapshot_hashes": [snapshot.content_hash],
        },
    )
    clone = replace(
        manifest,
        run_id=run_id,
        dataset=dataset,
        universe_version=snapshot.version,
    )
    repository.create_research_run(clone)
    return clone


def _pit_snapshot():
    member = UniverseMembership(
        universe="fixture-universe",
        symbol="600519.SH",
        effective_from=date(2020, 1, 1),
        effective_to=None,
        known_at=datetime.fromisoformat("2024-01-01T09:00:00+08:00"),
        exchange="SH",
        security_type="stock",
        industry="fixture",
        source="fixture-security-master",
        version="fixture-universe-v1",
        revision_id="r1-600519.SH",
    )
    return UniverseSnapshot(
        universe="fixture-universe",
        session_date=date(2024, 1, 2),
        knowledge_cutoff=datetime.fromisoformat("2024-01-02T15:00:00+08:00"),
        version="fixture-universe-v1",
        members=(member,),
    )


def test_governance_ignores_forged_claim_even_with_valid_lineage_and_hashes(lineage_client, monkeypatch):
    manifest, cutoff, original = _store_claim(lineage_client)
    runtime = lineage_client.app.state.runtime
    lineage_client.portal.call(runtime.monitor.stop)

    async def no_network(*args, **kwargs):
        raise AssertionError("governance cannot call upstream")

    monkeypatch.setattr(runtime.provider, "fetch_bars", no_network)
    monkeypatch.setattr(runtime.provider, "fetch_quotes", no_network)
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": cutoff.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "research_governance_v2"
    assert body["checks"]["lineage_verified"] is True
    assert body["artifacts_consistent"] is True
    assert body["declared_decision_grade"] is True
    assert body["reproducible"] is body["decision_ready"] is False
    assert body["checks"]["decision_grade"] is False
    assert body["checks"]["replay_verified"] is False
    assert body["replay_audit"]["verified"] is False
    assert "supported_strategy" in body["replay_audit"]["failures"]
    assert "caller_decision_grade_ignored" in body["warnings"]
    assert body["served_from"] == "sqlite_cache" and body["upstream_requests"] == 0
    assert runtime.repository.get_research_result(manifest.run_id).result_json == original


def test_governance_does_not_attest_replay_of_unbound_artifacts(lineage_client):
    manifest, _, _ = _store_claim(lineage_client, bind_batch=False)
    response = lineage_client.get("/api/v1/research/runs/{}/governance".format(manifest.run_id))
    body = response.json()
    assert body["artifacts_consistent"] is True
    assert body["reproducible"] is False
    assert "lineage_attestation_missing" in body["warnings"]


def test_comparison_does_not_promote_uploaded_decision_grade(lineage_client):
    manifest, _, original = _store_claim(lineage_client, bind_batch=False)
    repository = lineage_client.app.state.runtime.repository
    peer = replace(manifest, run_id=manifest.run_id + "-peer")
    repository.create_research_run(peer)
    response = lineage_client.get(
        "/api/v1/research/runs/compare",
        params={"run_ids": "{},{}".format(manifest.run_id, peer.run_id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    row, missing = body["runs"]
    assert row["declared_decision_grade"] is True
    assert row["decision_grade"] is False
    assert "decision_grade_unattested" in row["decision_grade_failures"]
    assert missing["result_available"] is False
    assert missing["decision_grade"] is None
    assert missing["declared_decision_grade"] is None
    assert body["upstream_requests"] == 0
    assert repository.get_research_result(manifest.run_id).result_json == original


@pytest.mark.parametrize("cutoff", ["invalid", "2024-01-03T15:00:00"])
def test_governance_rejects_invalid_cutoff_even_without_batch(lineage_client, cutoff):
    manifest, _, _ = _store_claim(lineage_client, bind_batch=False)
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": cutoff},
    )
    assert response.status_code == 422


def test_governance_rejects_truncated_lineage_scan(lineage_client, monkeypatch):
    manifest, cutoff, _ = _store_claim(lineage_client)
    repo = lineage_client.app.state.runtime.repository
    original = repo.query_bar_observations

    def truncated(*args, **kwargs):
        rows, total = original(*args, **kwargs)
        return rows, total + 1

    monkeypatch.setattr(repo, "query_bar_observations", truncated)
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": cutoff.isoformat()},
    )
    body = response.json()
    assert body["lineage_scan_truncated"] is True
    assert body["checks"]["lineage_verified"] is False
    assert "lineage_scan_truncated" in body["warnings"]


def test_governance_discloses_legacy_partial_metric_coverage(lineage_client):
    def legacy(payload):
        for name in (
            "closed_trade_count", "winning_trade_count", "losing_trade_count",
            "break_even_trade_count", "win_rate", "gross_profit", "gross_loss",
            "realized_pnl", "profit_factor", "expectancy_per_closed_trade",
            "trade_statistics_status", "profit_factor_status",
        ):
            payload["metrics"].pop(name)

    manifest, cutoff, original = _store_claim(lineage_client, mutate=legacy)
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": cutoff.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metric_audit"]["verified"] is True
    assert body["metric_audit"]["trade_statistics_status"] == "unavailable"
    assert body["metric_audit"]["unavailable_checks"]
    assert body["checks"]["result_metrics_integrity"] is False
    assert body["artifacts_consistent"] is False
    assert "result_metrics_coverage_incomplete" in body["warnings"]
    assert "result_metrics_integrity_failed" not in body["warnings"]
    assert lineage_client.app.state.runtime.repository.get_research_result(manifest.run_id).result_json == original


@pytest.mark.parametrize("alteration,failed_check", [
    ("strategy", "strategy_matches_manifest"),
    ("metrics", "result_metrics_integrity"),
])
def test_governance_valid_hash_does_not_hide_inconsistent_content(lineage_client, alteration, failed_check):
    def forge(payload):
        if alteration == "strategy":
            payload["strategy_name"] = "unrelated_strategy"
        else:
            payload["metrics"]["total_return"] = 100.0

    manifest, _, _ = _store_claim(lineage_client, mutate=forge)
    response = lineage_client.get("/api/v1/research/runs/{}/governance".format(manifest.run_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checks"]["result_integrity"] is True
    assert body["checks"][failed_check] is False
    assert body["artifacts_consistent"] is False
    assert body["reproducible"] is body["decision_ready"] is False


def test_governance_reports_persisted_pit_snapshot_evidence_without_promoting_grade(
    lineage_client, monkeypatch
):
    repository = lineage_client.app.state.runtime.repository
    snapshot = _pit_snapshot()
    decision_at = datetime.fromisoformat("2024-01-04T15:00:00+08:00")
    manifest = _pit_manifest(
        repository,
        recorded_at=decision_at - timedelta(hours=1),
        snapshot=snapshot,
        run_id="pit-governance-run",
    )
    repository.insert_universe_snapshots(
        [snapshot],
        source="fixture-universe",
        recorded_at=decision_at - timedelta(hours=1),
    )

    async def no_provider(*args, **kwargs):
        raise AssertionError("governance must use persisted PIT evidence only")

    monkeypatch.setattr(lineage_client.app.state.runtime.provider, "fetch_bars", no_provider)
    monkeypatch.setattr(lineage_client.app.state.runtime.provider, "fetch_quotes", no_provider)
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": decision_at.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    binding = body["universe_binding"]
    assert binding["required"] is True
    assert binding["verified"] is True
    assert binding["status"] == "verified"
    assert binding["declared_snapshot_hashes"] == [snapshot.content_hash]
    assert binding["matched_snapshot_hashes"] == [snapshot.content_hash]
    assert binding["snapshots"][0]["source"] == "fixture-universe"
    assert binding["snapshots"][0]["recorded_at"] == "2024-01-04T14:00:00+08:00"
    # Persisted PIT evidence is not sufficient to authenticate a result.
    assert body["checks"]["decision_grade"] is False
    assert body["decision_ready"] is False


def test_governance_marks_late_pit_snapshot_missing_at_decision_time(lineage_client):
    repository = lineage_client.app.state.runtime.repository
    snapshot = _pit_snapshot()
    decision_at = datetime.fromisoformat("2024-01-04T15:00:00+08:00")
    manifest = _pit_manifest(
        repository,
        recorded_at=decision_at + timedelta(minutes=1),
        snapshot=snapshot,
        run_id="pit-governance-late-run",
    )
    # The snapshot describes an older session, but it was only received after
    # the decision.  v9's as-of query must therefore hide it.
    repository.insert_universe_snapshots(
        [snapshot],
        source="late-fixture",
        recorded_at=decision_at + timedelta(minutes=1),
    )
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": decision_at.isoformat()},
    )
    assert response.status_code == 200, response.text
    binding = response.json()["universe_binding"]
    assert binding["verified"] is False
    assert binding["status"] == "missing"
    assert binding["missing_snapshot_hashes"] == [snapshot.content_hash]


def test_governance_treats_hash_only_universe_version_as_pit_declaration(lineage_client):
    repository = lineage_client.app.state.runtime.repository
    snapshot = _pit_snapshot()
    decision_at = datetime.fromisoformat("2024-01-04T15:00:00+08:00")
    base, _, _, _ = _persist_evidence(repository)
    # The manifest contract permits the content hash directly in
    # versions.universe.  It must not fall back to the legacy display-label
    # path merely because dataset.attributes has no hash list.
    dataset = replace(base.dataset, attributes={"batch_id": "lineage-api-batch"})
    manifest = replace(
        base,
        run_id="pit-governance-hash-only-run",
        dataset=dataset,
        universe_version=snapshot.content_hash,
    )
    repository.create_research_run(manifest)
    repository.insert_universe_snapshots(
        [snapshot],
        source="fixture-universe",
        recorded_at=decision_at - timedelta(hours=1),
    )
    response = lineage_client.get(
        "/api/v1/research/runs/{}/governance".format(manifest.run_id),
        params={"decision_at": decision_at.isoformat()},
    )
    assert response.status_code == 200, response.text
    binding = response.json()["universe_binding"]
    assert binding["required"] is True
    assert binding["status"] == "verified"
    assert binding["declared_snapshot_hashes"] == [snapshot.content_hash]
