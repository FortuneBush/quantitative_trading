"""Stable JSON adapters for domain objects."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Mapping, Sequence

from quant_monitor.domain import (
    AdjustmentFactor,
    Alert,
    Bar,
    BarObservation,
    CollectionBatch,
    CorporateAction,
    Metric,
    Quote,
    Signal,
    now_market_time,
)
from quant_monitor.research import (
    CanonicalizationError,
    ManifestValidationError,
    ResearchResultRecord,
    ResearchRunRecord,
    UniverseSnapshot,
    canonical_json,
    hash_canonical,
)
from quant_monitor.backtesting import BacktestResult


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        # ``dataclasses.asdict`` deep-copies values and raises for the
        # MappingProxyType used by immutable domain metadata.  Walking fields
        # directly preserves the same JSON shape without copying opaque
        # objects or accidentally invoking provider-specific serializers.
        return {
            item.name: jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__}
    return value


def _safe_lineage_metadata(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return JSON-safe lineage metadata with conservative secret redaction.

    Collection metadata is intentionally small and provider-neutral, but it
    is persisted by adapters and therefore should never be trusted as a safe
    public payload.  Scan both keys and values with the same credential/path
    policy as portable research manifests: a harmless key such as ``note``
    must not turn an upstream URL or exception into a public credential leak.
    Plain credential field labels remain visible with redacted values, but
    keys containing actual secrets or machine-local paths are omitted.
    A depth cap prevents a malformed metadata value from creating an enormous
    response or recursive structure.
    """

    if depth > 8:
        return "[truncated]"
    if key:
        try:
            canonical_json({key: None})
        except (CanonicalizationError, ManifestValidationError):
            return "[redacted]"
    if isinstance(value, Mapping):
        result = {}
        for item_key, item_value in value.items():
            # Do not stringify an arbitrary key object or retain a key whose
            # text itself is sensitive; redacting only its value still leaks.
            if type(item_key) is not str:
                continue
            try:
                canonical_json(item_key)
            except (CanonicalizationError, ManifestValidationError):
                continue
            result[item_key] = _safe_lineage_metadata(
                item_value, key=item_key, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _safe_lineage_metadata(item, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__}
    try:
        canonical_json(value)
    except (CanonicalizationError, ManifestValidationError):
        return "[redacted]"
    return jsonable(value)


def collection_batch_dict(batch: CollectionBatch) -> Dict[str, Any]:
    """Serialize a collection batch without exposing raw exception text."""

    return {
        "batch_id": batch.batch_id,
        "provider": batch.provider,
        "request_kind": batch.request_kind,
        "input_hash": batch.input_hash,
        "status": batch.status.value,
        "started_at": batch.started_at.isoformat(),
        "completed_at": (
            batch.completed_at.isoformat() if batch.completed_at is not None else None
        ),
        "item_count": batch.item_count,
        "error_type": batch.error_type,
        # Exception messages are intentionally not returned.  They can carry
        # request URLs, local paths, or provider credentials even when logs are
        # configured safely.
        "error_message_available": batch.error_message is not None,
        "metadata": _safe_lineage_metadata(batch.metadata),
    }


def bar_observation_dict(observation: BarObservation) -> Dict[str, Any]:
    """Serialize an immutable bar observation with explicit field semantics."""

    return {
        "batch_id": observation.batch_id,
        "symbol": observation.symbol,
        "interval": observation.interval,
        "session_date": observation.session_date.isoformat(),
        "period_start": observation.period_start.isoformat(),
        "period_end": observation.period_end.isoformat(),
        "available_at": (
            observation.available_at.isoformat()
            if observation.available_at is not None
            else None
        ),
        "received_at": observation.received_at.isoformat(),
        "persisted_at": observation.persisted_at.isoformat(),
        "source": observation.source,
        "adjustment": observation.adjustment.value,
        "revision_id": observation.revision_id,
        "quality_status": observation.quality_status.value,
        "open": str(observation.open),
        "high": str(observation.high),
        "low": str(observation.low),
        "close": str(observation.close),
        "volume": observation.volume,
        "turnover": str(observation.turnover),
    }


def corporate_action_dict(action: CorporateAction) -> Dict[str, Any]:
    """Serialize one immutable company action with its content identity."""

    return {
        "action_id": action.action_id,
        "symbol": action.symbol,
        "action_type": action.action_type.value,
        "ex_date": action.ex_date.isoformat(),
        "announcement_date": (
            action.announcement_date.isoformat()
            if action.announcement_date is not None else None
        ),
        "record_date": action.record_date.isoformat() if action.record_date else None,
        "payable_date": action.payable_date.isoformat() if action.payable_date else None,
        "cash_amount": str(action.cash_amount) if action.cash_amount is not None else None,
        "share_ratio": str(action.share_ratio) if action.share_ratio is not None else None,
        "subscription_price": (
            str(action.subscription_price)
            if action.subscription_price is not None else None
        ),
        "source": action.source,
        "revision_id": action.revision_id,
        "received_at": action.received_at.isoformat(),
        "metadata": _safe_lineage_metadata(action.metadata),
        "content_hash": action.content_hash,
    }


def adjustment_factor_dict(factor: AdjustmentFactor) -> Dict[str, Any]:
    """Serialize one immutable adjustment factor without materializing a bar."""

    return {
        "factor_id": factor.factor_id,
        "symbol": factor.symbol,
        "adjustment": factor.adjustment.value,
        "session_date": factor.session_date.isoformat(),
        "benchmark_date": factor.benchmark_date.isoformat(),
        "factor": str(factor.factor),
        "factor_version": factor.factor_version,
        "source": factor.source,
        "revision_id": factor.revision_id,
        "input_hash": factor.input_hash,
        "computed_at": factor.computed_at.isoformat(),
        "content_hash": factor.content_hash,
    }


def universe_snapshot_dict(snapshot: UniverseSnapshot) -> Dict[str, Any]:
    """Serialize a validated immutable point-in-time universe snapshot."""

    if not isinstance(snapshot, UniverseSnapshot):
        raise TypeError("snapshot must be a UniverseSnapshot")
    return snapshot.to_dict()


def research_run_dict(record: ResearchRunRecord) -> Dict[str, Any]:
    """Serialize a persisted manifest while preserving its content hash."""

    if not isinstance(record, ResearchRunRecord):
        raise TypeError("record must be a ResearchRunRecord")
    return {
        "run_id": record.run_id,
        "content_hash": record.content_hash,
        "manifest_json_hash": record.manifest_json_hash,
        "manifest_schema_version": record.manifest_schema_version,
        "strategy_name": record.strategy_name,
        "strategy_code_version": record.strategy_code_version,
        "dataset_name": record.dataset_name,
        "dataset_source": record.dataset_source,
        "input_hash": record.input_hash,
        "calendar_version": record.calendar_version,
        "universe_version": record.universe_version,
        "adjustment_version": record.adjustment_version,
        "constraint_version": record.constraint_version,
        "created_at": record.created_at.isoformat(),
        "manifest": json.loads(record.manifest_json),
    }


def research_result_dict(
    record: ResearchResultRecord,
    *,
    include_result: bool = True,
) -> Dict[str, Any]:
    """Serialize an immutable stored result and preserve its integrity hashes.

    ``include_result`` is explicit so callers listing many artifacts can use a
    compact projection without repeatedly sending potentially large equity
    curves/trade ledgers.  Detail and creation endpoints keep the historical
    default of returning the complete, integrity-checked payload.
    """

    if not isinstance(record, ResearchResultRecord):
        raise TypeError("record must be a ResearchResultRecord")
    payload = {
        "run_id": record.run_id,
        "result_hash": record.result_hash,
        "result_json_hash": record.result_json_hash,
        "created_at": record.created_at.isoformat(),
    }
    if include_result:
        payload["result"] = json.loads(record.result_json)
    return payload


def backtest_result_dict(result: BacktestResult) -> Dict[str, Any]:
    """Serialize a deterministic backtest result with a reproducibility hash.

    The projection contains only domain values already present in the result;
    it never includes provider credentials, local paths or runtime objects.
    ``result_hash`` is calculated over the complete normalized projection so a
    caller can compare two offline runs without relying on object identity.
    """

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be a BacktestResult")
    payload: Dict[str, Any] = {
        "strategy_name": result.strategy_name,
        "started_at": result.started_at.isoformat(),
        "ended_at": result.ended_at.isoformat(),
        "config": jsonable(result.config),
        "equity_curve": jsonable(result.equity_curve),
        "trades": jsonable(result.trades),
        "skipped_executions": jsonable(result.skipped_executions),
        "positions": jsonable(result.positions),
        "metrics": jsonable(result.metrics),
        "diagnostics": jsonable(result.diagnostics),
    }
    payload["result_hash"] = hash_canonical(payload, namespace="backtest-result-v1")
    return payload


def quote_dict(
    quote: Quote,
    name: str = None,
    stale_after_seconds: float = None,
) -> Dict[str, Any]:
    # A provider clock can occasionally jump ahead of the local clock (or
    # replay a malformed future-dated row).  ``max(0, age)`` keeps the legacy
    # non-negative age contract, but must not let such a row appear fresh.
    # Keep the tolerance aligned with Runtime.system_status, which classifies
    # timestamps more than five seconds ahead as ``future_timestamp``.
    future_timestamp_tolerance_seconds = 5.0
    previous = quote.previous_close
    change = quote.last - previous if previous is not None else None
    percentage = change / previous * Decimal("100") if previous else None
    result = jsonable(quote)
    raw_age_seconds = (now_market_time() - quote.timestamp).total_seconds()
    future_timestamp = raw_age_seconds < -future_timestamp_tolerance_seconds
    age_seconds = max(0.0, raw_age_seconds)
    stale = (
        future_timestamp
        or bool(stale_after_seconds is not None and age_seconds > float(stale_after_seconds))
    )
    freshness = (
        "future_timestamp"
        if future_timestamp
        else ("stale" if stale else "fresh")
    )
    result.update(
        {
            "name": name or quote.symbol,
            "price": str(quote.last),
            "prev_close": str(previous) if previous is not None else None,
            "change": str(change) if change is not None else None,
            "pct_change": float(percentage) if percentage is not None else None,
            "amount": str(quote.turnover),
            "updated_at": quote.timestamp.isoformat(),
            "data_age_seconds": round(age_seconds, 3),
            "stale": stale,
            "freshness": freshness,
            "future_timestamp": future_timestamp,
            "future_by_seconds": round(max(0.0, -raw_age_seconds), 3),
        }
    )
    return result


def bar_dict(bar: Bar) -> Dict[str, Any]:
    result = jsonable(bar)
    result["amount"] = str(bar.turnover)
    result["time"] = bar.timestamp.isoformat()
    return result


def alert_dict(alert: Alert) -> Dict[str, Any]:
    result = jsonable(alert)
    for field_name in ("title", "message", "category"):
        result[field_name] = _safe_lineage_metadata(getattr(alert, field_name))
    result["metadata"] = _safe_lineage_metadata(alert.metadata)
    result["severity"] = alert.level.value
    result["created_at"] = alert.timestamp.isoformat()
    return result


def signal_dict(signal: Signal) -> Dict[str, Any]:
    result = jsonable(signal)
    result["reason"] = _safe_lineage_metadata(signal.reason)
    result["strategy"] = _safe_lineage_metadata(signal.strategy)
    result["metadata"] = _safe_lineage_metadata(signal.metadata)
    result["strategy_name"] = result["strategy"]
    result["created_at"] = signal.timestamp.isoformat()
    result["research_assurance"] = {
        "research_only": True,
        "decision_ready": False,
        "orders_supported": False,
        # Live Signal records do not yet bind an immutable input dataset.
        # A code/parameter checksum, or a strategy's self-reported metadata,
        # cannot certify the data used for that particular evaluation.
        "input_lineage_status": "unavailable",
        "strategy_binding_status": _signal_binding_status(signal),
        "notice": "仅为历史研究输出；输入血缘未绑定，不构成交易指令。",
    }
    return result


def _signal_binding_status(signal: Signal) -> str:
    """Check descriptor consistency, not source authenticity or readiness."""

    binding = signal.metadata.get("strategy_binding")
    if binding is None:
        return "missing"
    expected_fields = {
        "schema_version", "strategy_name", "parameters", "code_identity_status",
        "code_version", "binding_hash",
    }
    if not isinstance(binding, Mapping) or set(binding) != expected_fields:
        return "invalid"
    if (
        binding.get("schema_version") != "live-strategy-binding-v1"
        or binding.get("strategy_name") != signal.strategy
        or not isinstance(binding.get("parameters"), Mapping)
    ):
        return "invalid"
    identity_status = binding.get("code_identity_status")
    version = binding.get("code_version")
    if identity_status == "unavailable":
        if version is not None:
            return "invalid"
    elif identity_status == "verified_local_source":
        if (
            not isinstance(version, str)
            or not version.startswith("sha256:")
            or len(version) != 71
            or any(character not in "0123456789abcdef" for character in version[7:])
        ):
            return "invalid"
    else:
        return "invalid"
    descriptor = {key: value for key, value in binding.items() if key != "binding_hash"}
    try:
        expected_hash = hash_canonical(descriptor, namespace="live-strategy-binding-v1")
    except (TypeError, ValueError, OverflowError):
        return "invalid"
    return "digest_consistent" if binding.get("binding_hash") == expected_hash else "invalid"
