from __future__ import annotations

import pytest

from quant_monitor.backtesting import (
    build_bootstrap_robustness_report,
    build_bootstrap_robustness_report_from_payload,
)


def _payload(returns):
    nav = 1000.0
    curve = [{"timestamp": "2024-01-01T15:00:00+08:00", "daily_return": 0.0, "nav": str(nav)}]
    for index, value in enumerate(returns, 1):
        nav *= 1.0 + value
        curve.append(
            {
                "timestamp": "2024-01-{:02d}T15:00:00+08:00".format(index + 1),
                "daily_return": value,
                "nav": str(nav),
            }
        )
    return {"equity_curve": curve}


def test_bootstrap_is_reproducible_and_serializable():
    payload = _payload([0.01, -0.005, 0.02, 0.0] * 10)
    first = build_bootstrap_robustness_report(
        payload, confidence=0.90, resamples=300, seed=17, minimum_observations=20
    )
    second = build_bootstrap_robustness_report(
        payload, confidence=0.90, resamples=300, seed=17, minimum_observations=20
    )
    assert first == second
    assert first.sample_sufficient is True
    assert first.total_return_interval[0] <= first.total_return_interval[1]
    assert first.maximum_drawdown_interval[0] <= first.maximum_drawdown_interval[1]
    assert first.to_dict()["research_only"] is True


def test_bootstrap_payload_adapter_and_seed_change():
    payload = _payload([0.01, -0.005, 0.02, 0.0] * 10)
    direct = build_bootstrap_robustness_report(payload, resamples=200, seed=1, minimum_observations=20)
    adapted = build_bootstrap_robustness_report_from_payload(
        payload, resamples=200, seed=2, minimum_observations=20
    )
    assert direct.observed_total_return == adapted.observed_total_return
    assert direct.total_return_interval != adapted.total_return_interval


def test_bootstrap_fails_closed_for_short_or_missing_observations():
    short = build_bootstrap_robustness_report(
        _payload([0.01, -0.01]), resamples=20, seed=0, minimum_observations=5
    )
    assert short.sample_sufficient is False
    assert short.total_return_interval == (None, None)
    assert "bootstrap_intervals_unavailable" in short.warnings

    malformed = _payload([0.01, 0.02, 0.0, 0.01, 0.03])
    malformed["equity_curve"][2]["daily_return"] = None
    report = build_bootstrap_robustness_report(
        malformed, resamples=20, seed=0, minimum_observations=4
    )
    assert report.sample_sufficient is False
    assert report.observed_total_return is None
    assert "missing_or_invalid_return_observations" in report.warnings


def test_bootstrap_rejects_invalid_controls_and_returns():
    payload = _payload([0.01] * 5)
    with pytest.raises(ValueError):
        build_bootstrap_robustness_report(payload, confidence=1.0)
    with pytest.raises(ValueError):
        build_bootstrap_robustness_report(payload, resamples=100001)
    with pytest.raises(TypeError):
        build_bootstrap_robustness_report(payload, seed=True)
    with pytest.raises(ValueError):
        build_bootstrap_robustness_report(_payload([-1.1] * 5), minimum_observations=1)
