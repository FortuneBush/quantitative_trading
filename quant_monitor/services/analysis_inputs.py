"""Bounded, cache-only selection of immutable inputs for live research.

Matching, revision/quality checks and conversion use the SAME observation
snapshot.  This is local input provenance, never an exchange attestation.
"""

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

from quant_monitor.domain import Adjustment, Bar, ObservationQualityStatus, ensure_market_time


ANALYSIS_OBSERVATION_SCAN_LIMIT = 10000


def _values(item: Any) -> Tuple[Any, ...]:
    return (item.open, item.high, item.low, item.close, item.volume, item.turnover)


def select_analysis_bars(
    repository: Any,
    symbol: str,
    interval: str,
    projected: Sequence[Bar],
    *,
    as_of: datetime,
) -> Tuple[List[Bar], Dict[str, Any]]:
    """Use accepted raw observations only for a fully reconciled window.

    All raw revisions/qualities in the requested window are inspected; a
    quality filter would hide conflicting or quarantined revisions.  A
    truncated scan cannot prove completeness.  Programming errors such as an
    unsupported repository argument are intentionally NOT swallowed.
    """

    cutoff = ensure_market_time(as_of)
    projected = list(projected)
    observations, total = [], 0
    sources = {bar.source.strip().casefold() for bar in projected}
    if projected:
        observations, total = repository.query_bar_observations(
            symbol=symbol,
            interval=interval,
            source=next(iter(sources)) if len(sources) == 1 else None,
            adjustment=Adjustment.RAW,
            period_from=min(bar.timestamp for bar in projected),
            period_to=max(bar.timestamp for bar in projected),
            limit=ANALYSIS_OBSERVATION_SCAN_LIMIT,
        )
    truncated = total != len(observations)
    by_key = defaultdict(list)
    for item in observations:
        by_key[(item.period_end, item.source.strip().casefold())].append(item)
    matched, mismatches, non_accepted, unavailable = 0, 0, 0, 0
    quality_counts: Counter = Counter()
    selected = []
    for bar in projected:
        candidates = by_key.get((bar.timestamp, bar.source.strip().casefold()), ())
        if not candidates:
            continue
        if any(_values(item) != _values(bar) for item in candidates):
            mismatches += 1
            continue
        matched += 1
        if any(item.quality_status != ObservationQualityStatus.ACCEPTED for item in candidates):
            non_accepted += 1
        # Receipt/persistence in the future is not usable even if the bar's
        # nominal period already ended.  No market-clock tolerance is applied
        # to these local evidence timestamps.
        if any(
            item.available_at is None or item.available_at > cutoff
            or item.received_at > cutoff or item.persisted_at > cutoff
            for item in candidates
        ):
            unavailable += 1
        item = max(candidates, key=lambda row: (
            row.received_at, row.persisted_at, row.revision_id, row.batch_id
        ))
        quality_counts[item.quality_status.value] += 1
        selected.append(item)
    duplicate_periods = len({bar.timestamp for bar in projected}) != len(projected)
    lineage_complete = bool(projected) and matched == len(projected) and not truncated
    quality_complete = (
        lineage_complete and not non_accepted and not unavailable
        and not duplicate_periods and len(sources) == 1
    )
    warnings = []
    if matched != len(projected):
        warnings.append("bar_projection_rows_without_immutable_observation")
    if mismatches:
        warnings.append("immutable_observation_content_mismatch")
    if non_accepted:
        warnings.append("immutable_observation_quality_not_accepted")
    if unavailable:
        warnings.append("immutable_observation_not_available_at_analysis_time")
    if truncated:
        warnings.append("immutable_observation_scan_truncated")
    if duplicate_periods:
        warnings.append("duplicate_projection_periods")
    if len(sources) > 1:
        warnings.append("mixed_projection_sources")
    if not projected:
        warnings.append("no_bars_available")
    provenance = {
        "projection": "sqlite_bars_v1",
        "authoritative": False,
        "served_item_count": len(projected),
        "immutable_observation_count": matched,
        "legacy_item_count": len(projected) - matched,
        "content_mismatch_count": mismatches,
        "lineage_complete": lineage_complete,
        "quality_complete": quality_complete,
        "non_accepted_observation_count": non_accepted,
        "unavailable_observation_count": unavailable,
        "quality_status_counts": dict(quality_counts),
        "observation_scan_total": total,
        "observation_scan_count": len(observations),
        "observation_scan_truncated": truncated,
        "selection_as_of": cutoff.isoformat(),
        "calculation_input": (
            "immutable_observations" if quality_complete else "legacy_bars_projection"
        ),
        "selected_observations": [
            {"batch_id": item.batch_id, "revision_id": item.revision_id,
             "period_end": item.period_end.isoformat(), "source": item.source}
            for item in selected
        ] if quality_complete else [],
        "warnings": warnings,
    }
    return (
        [item.to_legacy_bar() for item in selected] if quality_complete else projected,
        provenance,
    )
