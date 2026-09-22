"""Point-in-time bank fundamental factors for research-only ranking.

The module consumes already persisted :class:`FundamentalObservation` values;
it never fetches data or guesses missing financial metrics.  A score is only
published when every configured metric for a symbol is present with the
declared unit.  This deliberately favors an explicit unavailable state over a
partial score that looks more precise than its inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import FundamentalObservation, ensure_market_time, normalize_symbol


@dataclass(frozen=True)
class BankMetricSpec:
    """One canonical bank metric and its research interpretation."""

    name: str
    unit: str
    higher_is_better: bool
    weight: float

    def __post_init__(self) -> None:
        name = str(self.name).strip().lower()
        unit = str(self.unit).strip().lower()
        if not name or not unit:
            raise ValueError("metric name and unit cannot be empty")
        weight = float(self.weight)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("metric weight must be positive and finite")
        if not isinstance(self.higher_is_better, bool):
            raise TypeError("higher_is_better must be boolean")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "weight", weight)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "higher_is_better": self.higher_is_better,
            "weight": self.weight,
        }


# Values are intentionally named and weighted in code, not accepted as an
# arbitrary import path or expression from configuration.  Percent metrics
# must be stored as percentage points (for example 1.25 means 1.25%), while
# PB is a ratio (for example 0.65).  Providers with another convention must
# normalize it before persistence and record that conversion in metadata.
BANK_METRIC_SPECS: Tuple[BankMetricSpec, ...] = (
    BankMetricSpec("pb", "ratio", False, 0.20),
    BankMetricSpec("roe", "percent", True, 0.20),
    BankMetricSpec("dividend_yield", "percent", True, 0.15),
    BankMetricSpec("npl_ratio", "percent", False, 0.15),
    BankMetricSpec("provision_coverage", "percent", True, 0.10),
    BankMetricSpec("capital_adequacy", "percent", True, 0.10),
    BankMetricSpec("cost_income_ratio", "percent", False, 0.10),
)
_SPEC_BY_NAME = MappingProxyType({item.name: item for item in BANK_METRIC_SPECS})
_TOTAL_WEIGHT = sum(item.weight for item in BANK_METRIC_SPECS)


def _decimal(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("fundamental value must be a finite Decimal")
    return value


_EVIDENCE_FIELDS = frozenset({
    "observation_id", "content_hash", "period_start", "period_end", "available_at",
    "received_at", "unit", "scope", "source", "revision_id",
})


def _fact_evidence(fact: FundamentalObservation) -> Dict[str, str]:
    """Expose fixed provenance fields, never arbitrary provider metadata."""

    return {
        "observation_id": fact.observation_id,
        "content_hash": fact.content_hash,
        "period_start": fact.period_start.isoformat(),
        "period_end": fact.period_end.isoformat(),
        "available_at": fact.available_at.isoformat(),
        "received_at": fact.received_at.isoformat(),
        "unit": fact.unit,
        "scope": fact.scope,
        "source": fact.source,
        "revision_id": fact.revision_id,
    }


def _freeze_evidence(
    evidence: Mapping[str, Mapping[str, str]], decision_at: datetime,
) -> Mapping[str, Mapping[str, str]]:
    result = {}
    for metric, raw in evidence.items():
        if metric not in _SPEC_BY_NAME or not isinstance(raw, Mapping):
            raise ValueError("invalid bank metric evidence")
        if set(raw) != _EVIDENCE_FIELDS:
            raise ValueError("bank metric evidence requires fixed provenance fields")
        if any(not isinstance(value, str) or not value.strip() for value in raw.values()):
            raise ValueError("bank metric evidence fields must be nonempty strings")
        item = dict(raw)
        digest = item["content_hash"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("bank metric evidence requires a SHA-256 content hash")
        start = date.fromisoformat(item["period_start"])
        end = date.fromisoformat(item["period_end"])
        if start > end:
            raise ValueError("bank metric evidence period is inverted")
        available = ensure_market_time(datetime.fromisoformat(item["available_at"]))
        received = ensure_market_time(datetime.fromisoformat(item["received_at"]))
        if available > decision_at or received < available:
            raise ValueError("bank metric evidence has invalid availability times")
        if item["scope"] not in {"consolidated", "standalone", "unknown"}:
            raise ValueError("bank metric evidence has an invalid scope")
        item["period_start"] = start.isoformat()
        item["period_end"] = end.isoformat()
        item["available_at"] = available.isoformat()
        item["received_at"] = received.isoformat()
        result[metric] = MappingProxyType(item)
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class BankFundamentalFactor:
    """One symbol's as-of bank factor evidence."""

    symbol: str
    decision_at: datetime
    values: Mapping[str, Decimal]
    missing_metrics: Tuple[str, ...]
    invalid_unit_metrics: Tuple[str, ...]
    score: Optional[float]
    score_status: str
    source: Optional[str]
    revision_ids: Mapping[str, str]
    source_conflict: bool = False
    evidence: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    invalid_scope_metrics: Tuple[str, ...] = field(default_factory=tuple)
    incomparable_period_metrics: Tuple[str, ...] = field(default_factory=tuple)
    scope_conflict: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "decision_at", ensure_market_time(self.decision_at))
        values: Dict[str, Decimal] = {}
        for raw_name, raw_value in self.values.items():
            name = str(raw_name).strip().lower()
            if name not in _SPEC_BY_NAME:
                raise ValueError("unknown bank metric {}".format(name))
            values[name] = _decimal(raw_value)
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(values.items()))))
        evidence = _freeze_evidence(self.evidence, self.decision_at)
        object.__setattr__(self, "evidence", evidence)
        missing = tuple(dict.fromkeys(str(item) for item in self.missing_metrics))
        invalid = tuple(dict.fromkeys(str(item) for item in self.invalid_unit_metrics))
        invalid_scopes = tuple(dict.fromkeys(self.invalid_scope_metrics))
        incomparable_periods = tuple(dict.fromkeys(self.incomparable_period_metrics))
        if any(item not in _SPEC_BY_NAME for item in missing + invalid + invalid_scopes + incomparable_periods):
            raise ValueError("unknown metric in coverage state")
        if set(missing) != set(_SPEC_BY_NAME).difference(values):
            raise ValueError("missing metrics must match the absent values")
        if not set(values).issubset(evidence):
            raise ValueError("every factor value requires provenance evidence")
        for metric, item in evidence.items():
            fact = FundamentalObservation(
                observation_id=item["observation_id"], symbol=self.symbol, metric=metric,
                value=values.get(metric), unit=item["unit"], scope=item["scope"],
                period_start=date.fromisoformat(item["period_start"]),
                period_end=date.fromisoformat(item["period_end"]),
                available_at=datetime.fromisoformat(item["available_at"]),
                received_at=datetime.fromisoformat(item["received_at"]),
                source=item["source"], revision_id=item["revision_id"], metadata={},
            )
            if fact.content_hash != item["content_hash"]:
                raise ValueError("factor evidence content hash does not match its value")
        if set(invalid) != {
            metric for metric, item in evidence.items() if item["unit"] != _SPEC_BY_NAME[metric].unit
        }:
            raise ValueError("invalid unit metrics must match the evidence")
        if set(invalid_scopes) != {
            metric for metric, item in evidence.items() if item["scope"] == "unknown"
        }:
            raise ValueError("invalid scope metrics must match the evidence")
        object.__setattr__(self, "missing_metrics", missing)
        object.__setattr__(self, "invalid_unit_metrics", invalid)
        object.__setattr__(self, "invalid_scope_metrics", invalid_scopes)
        object.__setattr__(self, "incomparable_period_metrics", incomparable_periods)
        status = str(self.score_status).strip().lower()
        if status not in {"complete", "unavailable"}:
            raise ValueError("unsupported score_status")
        object.__setattr__(self, "score_status", status)
        if status == "complete":
            if self.score is None or not math.isfinite(float(self.score)):
                raise ValueError("complete factor must have a finite score")
            if missing or invalid or invalid_scopes or incomparable_periods:
                raise ValueError("complete factor cannot have missing or incomparable metrics")
            score = float(self.score)
            if not 0.0 <= score <= 1.0:
                raise ValueError("factor score must be between zero and one")
            object.__setattr__(self, "score", score)
        else:
            if self.score is not None:
                raise ValueError("unavailable factor cannot have a score")
            object.__setattr__(self, "score", None)
        source = None if self.source is None else str(self.source).strip()
        object.__setattr__(self, "source", source or None)
        if not isinstance(self.source_conflict, bool):
            raise TypeError("source_conflict must be boolean")
        if not isinstance(self.scope_conflict, bool):
            raise TypeError("scope_conflict must be boolean")
        sources = {item["source"] for item in evidence.values()}
        scopes = {item["scope"] for item in evidence.values()}
        expected_source = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else None)
        if self.source != expected_source:
            raise ValueError("factor source must match the evidence")
        if len(sources) > 1 and not self.source_conflict:
            raise ValueError("mixed factor sources must be disclosed")
        if len(scopes) > 1 and not self.scope_conflict:
            raise ValueError("mixed factor scopes must be disclosed")
        if status == "complete" and (self.source_conflict or self.scope_conflict):
            raise ValueError("complete factor cannot have mixed sources or scopes")
        revisions = {str(key): str(value) for key, value in self.revision_ids.items()}
        if revisions != {metric: item["revision_id"] for metric, item in evidence.items()}:
            raise ValueError("factor revisions must match the evidence")
        object.__setattr__(self, "revision_ids", MappingProxyType(dict(sorted(revisions.items()))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "decision_at": self.decision_at.isoformat(),
            "values": {key: str(value) for key, value in self.values.items()},
            "missing_metrics": list(self.missing_metrics),
            "invalid_unit_metrics": list(self.invalid_unit_metrics),
            "score": self.score,
            "score_status": self.score_status,
            "source": self.source,
            "revision_ids": dict(self.revision_ids),
            "source_conflict": self.source_conflict,
            "evidence": {metric: dict(item) for metric, item in self.evidence.items()},
            "invalid_scope_metrics": list(self.invalid_scope_metrics),
            "incomparable_period_metrics": list(self.incomparable_period_metrics),
            "scope_conflict": self.scope_conflict,
        }


@dataclass(frozen=True)
class BankFundamentalFactorReport:
    """Immutable cross-sectional factor report."""

    decision_at: datetime
    records: Tuple[BankFundamentalFactor, ...]
    metric_specs: Tuple[BankMetricSpec, ...] = BANK_METRIC_SPECS
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_at", ensure_market_time(self.decision_at))
        records = tuple(self.records)
        if any(not isinstance(item, BankFundamentalFactor) for item in records):
            raise TypeError("records must contain BankFundamentalFactor objects")
        if len({item.symbol for item in records}) != len(records):
            raise ValueError("factor records must have unique symbols")
        if any(item.decision_at != self.decision_at for item in records):
            raise ValueError("factor records must share the report decision time")
        if tuple(self.metric_specs) != BANK_METRIC_SPECS:
            raise ValueError("bank factor reports require the fixed metric specifications")
        object.__setattr__(self, "metric_specs", BANK_METRIC_SPECS)
        if any(item.score_status == "complete" for item in records):
            if len(records) < 2 or any(item.score_status != "complete" for item in records):
                raise ValueError("factor scores require a complete cross-section of at least two members")
            sources = {item["source"] for record in records for item in record.evidence.values()}
            scopes = {item["scope"] for record in records for item in record.evidence.values()}
            if len(sources) != 1 or len(scopes) != 1 or "unknown" in scopes:
                raise ValueError("factor scores require comparable sources and scopes")
            for spec in BANK_METRIC_SPECS:
                periods = {
                    (record.evidence[spec.name]["period_start"], record.evidence[spec.name]["period_end"])
                    for record in records
                }
                if len(periods) != 1:
                    raise ValueError("factor scores require comparable periods for each metric")
            for record in records:
                expected_score = sum(
                    _percentile(
                        tuple(item.values[spec.name] for item in records),
                        record.values[spec.name], higher_is_better=spec.higher_is_better,
                    ) * spec.weight for spec in BANK_METRIC_SPECS
                ) / _TOTAL_WEIGHT
                if not math.isclose(record.score, expected_score, rel_tol=0.0, abs_tol=1e-12):
                    raise ValueError("factor score does not match the cross-sectional values")
        object.__setattr__(self, "records", tuple(sorted(records, key=lambda item: item.symbol)))
        warnings = tuple(dict.fromkeys(str(item).strip() for item in self.warnings if str(item).strip()))
        object.__setattr__(self, "warnings", warnings)

    @property
    def complete_count(self) -> int:
        return sum(item.score_status == "complete" for item in self.records)

    @property
    def quality_status(self) -> str:
        if not self.records:
            return "empty"
        if self.complete_count == len(self.records):
            return "ok"
        return "partial"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_at": self.decision_at.isoformat(),
            "selection": "bank_fundamental_factors_as_of_v1",
            "metric_specs": [item.to_dict() for item in self.metric_specs],
            "items": [item.to_dict() for item in self.records],
            "count": len(self.records),
            "complete_count": self.complete_count,
            "quality": {"status": self.quality_status, "complete_count": self.complete_count},
            "warnings": list(self.warnings),
        }


def _percentile(values: Sequence[Decimal], target: Decimal, *, higher_is_better: bool) -> float:
    """Return a deterministic mid-rank in [0, 1] for a cross-section."""

    ordered = sorted(values, reverse=higher_is_better)
    if len(ordered) <= 1:
        return 1.0
    positions = [index for index, item in enumerate(ordered) if item == target]
    if not positions:
        raise ValueError("target value is absent from percentile population")
    return 1.0 - (sum(positions) / len(positions)) / (len(ordered) - 1)


def build_bank_fundamental_factors(
    observations: Iterable[FundamentalObservation],
    *,
    decision_at: datetime,
    symbols: Optional[Iterable[str]] = None,
) -> BankFundamentalFactorReport:
    """Build complete-only bank factor scores from point-in-time facts.

    ``fundamentals_as_of`` performs the temporal and revision selection.  A
    supplied ``symbols`` sequence keeps missing securities visible in the
    report instead of shrinking the denominator to survivors.
    """

    cutoff = ensure_market_time(decision_at)
    requested = None if symbols is None else tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))
    requested_set = None if requested is None else set(requested)
    selected = []
    for item in observations:
        if not isinstance(item, FundamentalObservation):
            raise TypeError("observations must contain FundamentalObservation objects")
        if item.metric in _SPEC_BY_NAME and (requested_set is None or item.symbol in requested_set):
            selected.append(item)
    from quant_monitor.domain import fundamentals_as_of

    visible = fundamentals_as_of(selected, decision_at=cutoff)
    by_symbol: Dict[str, Dict[str, FundamentalObservation]] = {}
    for item in visible:
        if item.metric in _SPEC_BY_NAME:
            by_symbol.setdefault(item.symbol, {})[item.metric] = item
    universe = sorted(by_symbol) if requested is None else requested
    prepared = []
    all_sources = set()
    all_scopes = set()
    periods_by_metric = {spec.name: set() for spec in BANK_METRIC_SPECS}
    for symbol in universe:
        facts = by_symbol.get(symbol, {})
        values: Dict[str, Decimal] = {}
        missing = []
        invalid_units = []
        invalid_scopes = []
        revisions: Dict[str, str] = {}
        sources = set()
        evidence = {}
        for spec in BANK_METRIC_SPECS:
            fact = facts.get(spec.name)
            if fact is None:
                missing.append(spec.name)
                continue
            evidence[spec.name] = _fact_evidence(fact)
            revisions[spec.name] = fact.revision_id
            sources.add(fact.source)
            all_scopes.add(fact.scope)
            periods_by_metric[spec.name].add((fact.period_start, fact.period_end))
            if fact.value is None:
                missing.append(spec.name)
            else:
                values[spec.name] = fact.value
            if fact.unit != spec.unit:
                invalid_units.append(spec.name)
            if fact.scope == "unknown":
                invalid_scopes.append(spec.name)
        all_sources.update(sources)
        prepared.append((symbol, facts, values, missing, invalid_units, revisions, sources, evidence, invalid_scopes))

    incomparable_periods = tuple(spec.name for spec in BANK_METRIC_SPECS if len(periods_by_metric[spec.name]) > 1)
    scope_conflict = len(all_scopes) > 1

    # A cross-sectional score must not silently shrink its denominator to the
    # symbols that happen to have all facts.  If any requested member is
    # incomplete, expose all values and coverage reasons but keep every score
    # unavailable until the universe is complete.
    cross_section_complete = (
        len(universe) >= 2 and len(all_sources) == 1 and len(all_scopes) == 1
        and "unknown" not in all_scopes and not incomparable_periods
        and all(not item[3] and not item[4] for item in prepared)
    )
    populations: Dict[str, Tuple[Decimal, ...]] = {}
    if cross_section_complete:
        for spec in BANK_METRIC_SPECS:
            population = tuple(item[1][spec.name].value for item in prepared)
            if any(value is None for value in population):
                cross_section_complete = False
                break
            populations[spec.name] = population

    records = []
    for symbol, facts, values, missing, invalid_units, revisions, sources, evidence, invalid_scopes in prepared:
        source_conflict = len(sources) > 1 or len(all_sources) > 1
        score = None
        complete = (
            cross_section_complete
            and not source_conflict
            and not missing
            and not invalid_units
        )
        if complete:
            score = sum(
                _percentile(
                    populations[spec.name],
                    facts[spec.name].value,
                    higher_is_better=spec.higher_is_better,
                )
                * spec.weight
                for spec in BANK_METRIC_SPECS
            ) / _TOTAL_WEIGHT
        records.append(
            BankFundamentalFactor(
                symbol=symbol,
                decision_at=cutoff,
                values=values,
                missing_metrics=tuple(missing),
                invalid_unit_metrics=tuple(invalid_units),
                score=score,
                score_status="complete" if complete else "unavailable",
                source=next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else None),
                revision_ids=revisions,
                source_conflict=source_conflict,
                evidence=evidence,
                invalid_scope_metrics=tuple(invalid_scopes),
                incomparable_period_metrics=incomparable_periods,
                scope_conflict=scope_conflict,
            )
        )
    warnings = []
    if not records:
        warnings.append("fundamental_observations_unavailable")
    if records and len(universe) < 2:
        warnings.append("fundamental_cross_section_too_small")
    if len(all_sources) > 1:
        warnings.append("fundamental_sources_mixed")
    if scope_conflict or "unknown" in all_scopes:
        warnings.append("fundamental_scopes_incomparable")
    if incomparable_periods:
        warnings.append("fundamental_periods_incomparable")
    if any(item.missing_metrics or item.invalid_unit_metrics for item in records):
        warnings.append("fundamental_metric_coverage_incomplete")
    return BankFundamentalFactorReport(cutoff, tuple(records), warnings=tuple(warnings))


__all__ = [
    "BANK_METRIC_SPECS",
    "BankFundamentalFactor",
    "BankFundamentalFactorReport",
    "BankMetricSpec",
    "build_bank_fundamental_factors",
]
