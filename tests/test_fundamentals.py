from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import pytest

from quant_monitor.domain import FundamentalObservation, fundamentals_as_of


def _obs(
    observation_id: str,
    value: Optional[str],
    available: str,
    revision: str,
    *,
    metric: str = "roe",
    period_start: date = date(2025, 1, 1),
    period_end: date = date(2025, 12, 31),
    unit: str = "percent",
    scope: str = "consolidated",
    source: str = "fixture",
    metadata=None,
) -> FundamentalObservation:
    return FundamentalObservation(
        observation_id=observation_id,
        symbol="600036.SH",
        metric=metric,
        value=None if value is None else Decimal(value),
        unit=unit,
        scope=scope,
        period_start=period_start,
        period_end=period_end,
        available_at=datetime.fromisoformat(available),
        received_at=datetime.fromisoformat(available),
        source=source,
        revision_id=revision,
        metadata={"fixture": True} if metadata is None else metadata,
    )


def test_fundamentals_as_of_excludes_future_and_selects_latest_revision():
    observations = [
        _obs("old", "10", "2026-02-01T09:00:00+08:00", "r1"),
        _obs("new", "11", "2026-03-01T09:00:00+08:00", "r2"),
        _obs("future", "99", "2026-04-01T09:00:00+08:00", "r3"),
        _obs("other", "5", "2026-02-01T09:00:00+08:00", "r1", metric="npl_ratio"),
    ]
    selected = fundamentals_as_of(
        observations,
        decision_at=datetime.fromisoformat("2026-03-15T15:00:00+08:00"),
    )
    assert [(item.metric, item.value) for item in selected] == [
        ("npl_ratio", Decimal("5")),
        ("roe", Decimal("11")),
    ]


def test_fundamentals_as_of_is_deterministic_for_same_available_time():
    with pytest.raises(ValueError, match="conflicting fundamental revisions"):
        fundamentals_as_of(
            [
                _obs("a", "10", "2026-02-01T09:00:00+08:00", "r1"),
                _obs("b", "12", "2026-02-01T09:00:00+08:00", "r2"),
            ],
            decision_at=datetime.fromisoformat("2026-02-02T15:00:00+08:00"),
        )


def test_same_time_equal_value_revisions_remain_deterministic():
    selected = fundamentals_as_of(
        [
            _obs("a", "10", "2026-02-01T09:00:00+08:00", "r1"),
            _obs("b", "10.00", "2026-02-01T09:00:00+08:00", "r2"),
        ],
        decision_at=datetime.fromisoformat("2026-02-02T15:00:00+08:00"),
    )
    assert selected[0].revision_id == "r2"


def test_reused_observation_id_with_different_economic_content_fails_closed():
    # A provider revision identifier must not be reused for another fact.  If
    # this slipped through, a period or metric filter could hide the conflict
    # and make the research input non-reproducible.
    with pytest.raises(ValueError, match="conflicting fundamental observation_id"):
        fundamentals_as_of(
            [
                _obs("reused", "10", "2026-02-01T09:00:00+08:00", "r1"),
                _obs(
                    "reused",
                    "11",
                    "2026-02-02T09:00:00+08:00",
                    "r2",
                    metric="npl_ratio",
                ),
            ],
            decision_at=datetime.fromisoformat("2026-03-01T15:00:00+08:00"),
        )


def test_late_restatement_of_old_period_cannot_displace_newer_report():
    selected = fundamentals_as_of(
        [
            _obs(
                "new-report",
                "12",
                "2026-02-01T09:00:00+08:00",
                "r1",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
            ),
            _obs(
                "old-restated",
                "8",
                "2026-03-01T09:00:00+08:00",
                "r9",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
            ),
        ],
        decision_at=datetime.fromisoformat("2026-03-15T15:00:00+08:00"),
    )
    assert selected[0].observation_id == "new-report"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"unit": "ratio"},
        {"scope": "standalone"},
        {"source": "other-vendor"},
    ],
)
def test_incomparable_dimensions_fail_closed(kwargs):
    observations = [
        _obs("a", "10", "2026-02-01T09:00:00+08:00", "r1"),
        _obs("b", "11", "2026-02-02T09:00:00+08:00", "r2", **kwargs),
    ]
    with pytest.raises(ValueError, match="ambiguous fundamental dimensions"):
        fundamentals_as_of(
            observations,
            decision_at=datetime.fromisoformat("2026-03-01T15:00:00+08:00"),
        )


def test_explicit_dimension_and_period_filters_disambiguate_query():
    selected = fundamentals_as_of(
        [
            _obs("consolidated", "10", "2026-02-01T09:00:00+08:00", "r1"),
            _obs(
                "standalone",
                "9",
                "2026-02-02T09:00:00+08:00",
                "r2",
                scope="standalone",
            ),
        ],
        decision_at=datetime.fromisoformat("2026-03-01T15:00:00+08:00"),
        scope="standalone",
        unit="percent",
        source="fixture",
        period_end=date(2025, 12, 31),
    )
    assert selected[0].observation_id == "standalone"


def test_partial_period_filter_still_prefers_newest_matching_period():
    selected = fundamentals_as_of(
        [
            _obs(
                "first-half",
                "10",
                "2026-02-01T09:00:00+08:00",
                "r1",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 6, 30),
            ),
            _obs(
                "full-year",
                "12",
                "2026-03-01T09:00:00+08:00",
                "r2",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
            ),
        ],
        decision_at=datetime.fromisoformat("2026-04-01T15:00:00+08:00"),
        period_start=date(2025, 1, 1),
    )
    assert selected[0].observation_id == "full-year"


def test_metadata_is_deeply_immutable_and_to_dict_is_detached():
    metadata = {"nested": {"tags": ["bank"]}}
    observation = _obs("immutable", "10", "2026-02-01T09:00:00+08:00", "r1", metadata=metadata)
    metadata["nested"]["tags"].append("mutated")
    with pytest.raises(TypeError):
        observation.metadata["nested"]["tags"] += ("blocked",)
    payload = observation.to_dict()
    payload["metadata"]["nested"]["tags"].append("detached")
    assert observation.metadata["nested"]["tags"] == ("bank",)


def test_fundamental_observation_rejects_invalid_temporal_and_metric_data():
    with pytest.raises(ValueError, match="metric"):
        _obs("bad", "1", "2026-02-01T09:00:00+08:00", "r1", metric="ROE!")
    with pytest.raises(ValueError, match="received_at"):
        FundamentalObservation(
            observation_id="late",
            symbol="600036.SH",
            metric="roe",
            value=Decimal("1"),
            unit="percent",
            scope="consolidated",
            period_start=datetime.fromisoformat("2025-01-01").date(),
            period_end=datetime.fromisoformat("2025-12-31").date(),
            available_at=datetime.fromisoformat("2026-03-01T09:00:00+08:00"),
            received_at=datetime.fromisoformat("2026-02-01T09:00:00+08:00"),
            source="fixture",
            revision_id="r1",
            metadata={},
        )
