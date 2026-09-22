"""Pure, fail-closed verification of persisted research backtest artifacts.

The verifier intentionally accepts all evidence as arguments.  It has no
repository/provider/clock access, and therefore can be used by governance
endpoints without creating a hidden refresh path.  A replay is only attested
when lineage, point-in-time availability, manifest bindings and the complete
serialized result all agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
import re

from quant_monitor.domain import BarObservation, CollectionBatch

from .lineage_gate import audit_observation_lineage
from .code_identity import strategy_code_version
from .manifest import ResearchRunRecord, hash_canonical


_STRATEGY = "bank_daily_multifactor_v1"
_STRATEGY_FIELDS = {
    "min_history", "min_universe", "entry_percentile", "exit_percentile",
    "minimum_breadth", "maximum_annualized_volatility", "maximum_drawdown",
    "liquidity_floor_percentile", "corporate_action_gap", "trend_exit_days",
    "valid_sessions",
}
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _strategy_parameters(value: Any) -> Optional[Mapping[str, Any]]:
    """Accept supported parameters, never silently discard unknown fields.

    Retain the historical ``{"parameters": {...}}`` wrapper only when it is
    unambiguous. Mixing it with direct parameters must not choose one set and
    silently ignore the other.
    """

    if not isinstance(value, Mapping):
        return None
    if set(value) == {"parameters"}:
        value = value["parameters"]
    if not isinstance(value, Mapping) or not set(value).issubset(_STRATEGY_FIELDS):
        return None
    return value


def _snapshot_evidence_by_session(
    value: Any,
) -> Optional[Dict[Any, Any]]:
    """Normalize persisted PIT snapshots supplied by an audit caller.

    Repository reads naturally return a list, while callers that already
    joined the immutable rows to bars commonly pass ``{session: snapshot}``.
    Accept both forms at this pure boundary, but never infer a session from a
    list position and never accept duplicate/conflicting rows.  The values
    are checked as ``UniverseSnapshot`` objects here; this keeps malformed
    provider-shaped payloads from being treated as research evidence.
    """

    from datetime import date

    from .universe import UniverseSnapshot

    if isinstance(value, Mapping):
        rows = []
        for raw_session, snapshot in value.items():
            if isinstance(raw_session, datetime):
                return None
            if isinstance(raw_session, date):
                session = raw_session
            elif isinstance(raw_session, str):
                try:
                    session = date.fromisoformat(raw_session)
                except (TypeError, ValueError):
                    return None
            else:
                return None
            if not isinstance(snapshot, UniverseSnapshot):
                return None
            if snapshot.session_date != session:
                return None
            rows.append((session, snapshot))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows = []
        for snapshot in value:
            if not isinstance(snapshot, UniverseSnapshot):
                return None
            rows.append((snapshot.session_date, snapshot))
    else:
        return None

    normalized: Dict[Any, Any] = {}
    for session, snapshot in rows:
        prior = normalized.get(session)
        if prior is not None and prior.content_hash != snapshot.content_hash:
            return None
        if prior is not None:
            # Duplicate delivery of the same immutable row is harmless, but
            # it must be byte-identical (the hash check above is intentional).
            continue
        normalized[session] = snapshot
    return normalized


@dataclass(frozen=True)
class ReplayAuditReport:
    """Immutable, explainable result of an independent replay attempt."""

    checks: Mapping[str, bool]
    expected_result_hash: Optional[str] = None
    replayed_result_hash: Optional[str] = None
    # Public consumers only need a stable failure class.  Never retain the
    # exception message here: malformed manifests or provider-shaped fixture
    # values can contain URLs, local paths, response fragments, or credentials.
    error: Optional[str] = None
    failures: Tuple[str, ...] = field(init=False)
    verified: bool = field(init=False)

    def __post_init__(self) -> None:
        checks_value = self.checks
        if not isinstance(checks_value, Mapping) or not checks_value:
            raise ValueError("checks must be a non-empty mapping")
        normalized = {}
        for key, value in checks_value.items():
            if not isinstance(key, str) or type(value) is not bool:
                raise TypeError("replay checks must be string/bool pairs")
            normalized[key] = value
        checks = MappingProxyType(dict(sorted(normalized.items())))
        object.__setattr__(self, "checks", checks)
        failures = tuple(key for key, value in checks.items() if not value)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "verified", not failures)

    def to_dict(self) -> dict:
        return {
            "verified": self.verified,
            "replay_verified": self.verified,
            "checks": dict(self.checks),
            "failures": list(self.failures),
            "expected_result_hash": self.expected_result_hash,
            "replayed_result_hash": self.replayed_result_hash,
            "result_hash_matches": self.checks.get("result_hash_matches", False),
            "error": self.error,
        }

    @property
    def replay_verified(self) -> bool:
        return self.verified


def verify_replay(
    run_record: ResearchRunRecord,
    manifest: Mapping[str, Any],
    result_payload: Mapping[str, Any],
    observations: Sequence[BarObservation],
    batches: Iterable[CollectionBatch],
    decision_at: Optional[datetime],
    strategy_config: Optional[Mapping[str, Any]] = None,
    point_in_time_universe: Optional[object] = None,
    universe_knowledge_cutoff: Optional[datetime] = None,
    require_universe_binding: bool = False,
    require_calendar_binding: bool = False,
) -> ReplayAuditReport:
    """Replay and verify a run using only supplied immutable evidence.

    Unknown strategies, malformed evidence and any execution exception fail
    closed and are represented in the report rather than being coerced.
    ``strategy_config`` is a compatibility assertion, not an override: when
    supplied, it must exactly match the parameters bound by the manifest.
    Strict research callers may set ``require_universe_binding`` and provide
    the exact :class:`UniverseSnapshot`, a per-session sequence/mapping of
    persisted snapshots, or a resolver plus cutoff used to construct the
    bars.  The manifest's ``versions.universe`` must then carry the
    content-addressed snapshot hash (single snapshot), or the resolver/snapshot
    version together with ``dataset.attributes.universe_snapshot_hashes``.
    ``require_calendar_binding`` is the strict counterpart for the trading
    calendar.  It requires every replay batch to carry the immutable calendar
    version metadata declared by the manifest; ordinary compatibility replays
    keep the historical permissive behavior when this flag is false.
    """

    checks = {
        "manifest_integrity": False,
        "supported_strategy": False,
        "strategy_code_version_matches": False,
        "result_integrity": False,
        "strategy_matches_manifest": False,
        "strategy_configuration_bound": False,
        "execution_configuration_supported": False,
        "successful_collection_batches": False,
        "batch_item_count_matches": False,
        "accepted_quality": False,
        "availability_attested": False,
        "session_dates_valid": False,
        "unique_revision_keys": False,
        "input_hash_matches": False,
        "source_matches": False,
        "batch_provider_matches": False,
        "calendar_binding_matches": False,
        "response_receipt_consistent": False,
        "universe_binding_matches": not require_universe_binding,
        "interval_matches": False,
        "record_count_matches": False,
        "replay_result_hash_matches": False,
        "result_hash_matches": False,
    }
    expected_hash = None
    replay_hash = None
    error = None
    try:
        if not isinstance(run_record, ResearchRunRecord):
            raise TypeError("run_record must be a ResearchRunRecord")
        if not isinstance(manifest, Mapping) or not isinstance(result_payload, Mapping):
            raise TypeError("manifest and result_payload must be mappings")
        strategy = manifest.get("strategy")
        dataset = manifest.get("dataset")
        if not isinstance(strategy, Mapping) or not isinstance(dataset, Mapping):
            raise ValueError("manifest strategy/dataset sections are required")
        unsigned = dict(manifest)
        content_hash = unsigned.pop("content_hash", None)
        unsigned.pop("run_id", None)
        # ResearchRunManifest hashes a canonical content projection; checking
        # the persisted record's hash catches detached/forged parsed manifests.
        checks["manifest_integrity"] = (
            manifest.get("run_id") == run_record.run_id
            and content_hash == run_record.content_hash
            and manifest.get("input_hash") == run_record.input_hash
            and hash_canonical(unsigned, namespace="research-run-manifest-content-v2")
            == run_record.content_hash
        )
        strategy_name = strategy.get("name")
        checks["supported_strategy"] = strategy_name == _STRATEGY
        # A human label (for example ``v1``) is not an implementation
        # identity.  Only the content-addressed version emitted by
        # ``strategy_code_version`` can attest that this process is replaying
        # the same built-in source units as the persisted run.  Computing the
        # identity is deliberately fail-closed: a partial/damaged checkout
        # must never be treated as reproducible.
        checks["strategy_code_version_matches"] = (
            isinstance(strategy.get("code_version"), str)
            and strategy.get("code_version") == strategy_code_version(_STRATEGY)
        )
        checks["strategy_matches_manifest"] = result_payload.get("strategy_name") == strategy_name
        expected_hash = result_payload.get("result_hash") if isinstance(result_payload.get("result_hash"), str) else None
        unsigned_result = dict(result_payload)
        unsigned_result.pop("result_hash", None)
        checks["result_integrity"] = bool(expected_hash) and hash_canonical(
            unsigned_result, namespace="backtest-result-v1"
        ) == expected_hash
        observations = tuple(observations) if isinstance(observations, Sequence) else ()
        batch_values = tuple(batches) if batches is not None else ()
        declared_count = dataset.get("record_count")
        source = str(dataset.get("source", "")).strip()
        interval = str(dataset.get("interval", dataset.get("attributes", {}).get("interval", "1d"))).strip().lower()
        checks["interval_matches"] = bool(interval) and bool(observations) and all(
            isinstance(item, BarObservation) and item.interval == interval for item in observations
        )
        lineage = audit_observation_lineage(
            observations,
            expected_input_hash=run_record.input_hash,
            expected_record_count=declared_count if type(declared_count) is int else None,
            expected_source=source or None,
            decision_at=decision_at,
            batches=batch_values,
        )
        checks["successful_collection_batches"] = lineage.checks.get("successful_collection_batches", False)
        checks["batch_item_count_matches"] = lineage.checks.get("batch_item_count_matches", False)
        if len({getattr(item, "batch_id", object()) for item in batch_values}) != len(batch_values):
            checks["successful_collection_batches"] = False
        # A manifest's dataset source identifies the provider family used to
        # collect the immutable observations.  Checking only observation
        # source is insufficient: a forged/rebound batch could otherwise
        # satisfy the lineage gate while carrying a different provider's
        # request metadata.
        checks["batch_provider_matches"] = bool(batch_values) and bool(source) and all(
            str(getattr(batch, "provider", "")).strip().casefold() == source
            for batch in batch_values
        )

        # Collection batches disclose the calendar identity used to classify
        # rows.  Older batches may not have this optional metadata, so they
        # remain replay-compatible; when present, however, a mismatch with
        # the immutable manifest is a hard failure rather than a warning.
        versions = manifest.get("versions")
        expected_calendar_version = (
            versions.get("calendar") if isinstance(versions, Mapping) else None
        )
        calendar_payloads = []
        for batch in batch_values:
            metadata = getattr(batch, "metadata", {})
            calendar_payload = metadata.get("calendar") if isinstance(metadata, Mapping) else None
            if calendar_payload is not None:
                calendar_payloads.append(calendar_payload)
        if not calendar_payloads:
            # A manifest calendar version is only a declaration until the
            # collection batch records the calendar identity used to classify
            # the observations.  Compatibility callers may still replay old
            # batches without that optional metadata, but strict governance
            # must fail closed instead of treating absent evidence as proof.
            checks["calendar_binding_matches"] = not require_calendar_binding
        else:
            dataset_attributes = dataset.get("attributes", {})
            declared_fingerprint = (
                dataset_attributes.get("calendar_fingerprint")
                if isinstance(dataset_attributes, Mapping)
                else None
            )
            checks["calendar_binding_matches"] = bool(expected_calendar_version) and all(
                isinstance(payload, Mapping)
                and payload.get("version") == expected_calendar_version
                and (
                    declared_fingerprint is None
                    or payload.get("fingerprint") == declared_fingerprint
                )
                for payload in calendar_payloads
            )

        # A response digest is a receipt, not a vendor signature.  It is
        # nevertheless useful for detecting a batch whose metadata was
        # rebound after collection.  Validate its shape/scope and, when a
        # manifest declares an expected receipt, require exact agreement.
        receipt_values = []
        receipt_valid = True
        for batch in batch_values:
            metadata = getattr(batch, "metadata", {})
            if not isinstance(metadata, Mapping):
                continue
            receipt = metadata.get("response_content_hash")
            scope = metadata.get("response_content_hash_scope")
            if receipt is None:
                continue
            if not isinstance(receipt, str) or not _SHA256_HEX_RE.fullmatch(receipt.lower()):
                receipt_valid = False
            if scope != "parsed_json_response":
                receipt_valid = False
            receipt_values.append(receipt.lower() if isinstance(receipt, str) else None)
        attrs = dataset.get("attributes", {})
        expected_receipt = attrs.get("response_content_hash") if isinstance(attrs, Mapping) else None
        if expected_receipt is not None:
            checks["response_receipt_consistent"] = (
                receipt_valid
                and bool(receipt_values)
                and isinstance(expected_receipt, str)
                and _SHA256_HEX_RE.fullmatch(expected_receipt.lower()) is not None
                and all(value == expected_receipt.lower() for value in receipt_values)
            )
        else:
            # Without a manifest-declared receipt, absence is compatible with
            # legacy batches, but supplied receipts must still be valid and
            # mutually consistent.  Keep this decision independent from the
            # optional PIT-universe branch below.
            checks["response_receipt_consistent"] = (
                receipt_valid and len(set(receipt_values)) <= 1
            )

        # ``universe_version`` used to be a display label only.  A manifest
        # that declares per-session snapshot hashes is making a stronger
        # point-in-time claim, however: silently replaying it without the
        # resolver/snapshot would otherwise leave the check at its default
        # (the historical compatibility path) and attest a universe that was
        # never actually supplied to the replay.  Treat the declaration as
        # an implicit requirement for PIT evidence; callers that do not make
        # that declaration remain backwards compatible.
        attributes = dataset.get("attributes", {})
        declared_hashes = (
            attributes.get("universe_snapshot_hashes")
            if isinstance(attributes, Mapping) else None
        )
        universe_binding_required = require_universe_binding or declared_hashes is not None
        if universe_binding_required and point_in_time_universe is None:
            checks["universe_binding_matches"] = False
        elif point_in_time_universe is not None:
            from .universe import PointInTimeUniverse, UniverseSnapshot

            expected_universe = versions.get("universe") if isinstance(versions, Mapping) else None
            declared_hashes = (
                attributes.get("universe_snapshot_hashes")
                if isinstance(attributes, Mapping) else None
            )
            if isinstance(point_in_time_universe, UniverseSnapshot):
                checks["universe_binding_matches"] = (
                    expected_universe == point_in_time_universe.content_hash
                    and (declared_hashes is None or declared_hashes == [point_in_time_universe.content_hash])
                )
            elif isinstance(point_in_time_universe, PointInTimeUniverse):
                if universe_knowledge_cutoff is None:
                    checks["universe_binding_matches"] = False
                else:
                    try:
                        sessions = sorted({item.session_date for item in observations})
                        snapshots = [
                            point_in_time_universe.snapshot_at(session, universe_knowledge_cutoff)
                            for session in sessions
                        ]
                        hashes = sorted(item.content_hash for item in snapshots)
                        checks["universe_binding_matches"] = (
                            expected_universe == point_in_time_universe.version
                            and isinstance(declared_hashes, (list, tuple))
                            and list(declared_hashes) == hashes
                        )
                    except Exception:
                        checks["universe_binding_matches"] = False
            else:
                # Persisted repositories return immutable snapshots rather
                # than a live resolver.  Accept either a sequence or an
                # explicit session mapping, then bind every observed session
                # to exactly one snapshot.  This is deliberately stricter
                # than a version label: a list of hashes is not enough unless
                # the corresponding snapshot rows are actually supplied.
                snapshot_by_session = _snapshot_evidence_by_session(
                    point_in_time_universe
                )
                sessions = sorted({item.session_date for item in observations})
                if snapshot_by_session is not None and sessions:
                    try:
                        exact_sessions = set(snapshot_by_session) == set(sessions)
                        snapshots = [snapshot_by_session[session] for session in sessions]
                        versions_seen = {snapshot.version for snapshot in snapshots}
                        expected_version = (
                            "mixed" if len(versions_seen) > 1 else next(iter(versions_seen))
                        )
                        # A single immutable snapshot may be bound directly
                        # by its content hash in ``versions.universe``.  A
                        # multi-session mapping cannot use one hash label to
                        # describe all rows, so it must carry the resolver
                        # version (or ``mixed``) instead.
                        version_matches = (
                            (len(snapshots) == 1 and expected_universe == snapshots[0].content_hash)
                            or expected_universe == expected_version
                        )
                        hash_values = [snapshot.content_hash for snapshot in snapshots]
                        declared_matches = False
                        if isinstance(declared_hashes, Mapping):
                            expected_by_session = {}
                            for raw_session, raw_hash in declared_hashes.items():
                                if isinstance(raw_session, datetime):
                                    raise ValueError("universe snapshot session key is invalid")
                                session = raw_session
                                if isinstance(raw_session, str):
                                    from datetime import date

                                    session = date.fromisoformat(raw_session)
                                if not hasattr(session, "isoformat") or not isinstance(raw_hash, str):
                                    raise ValueError("universe snapshot hash mapping is invalid")
                                expected_by_session[session] = raw_hash
                            declared_matches = (
                                set(expected_by_session) == set(sessions)
                                and all(
                                    expected_by_session[session]
                                    == snapshot_by_session[session].content_hash
                                    for session in sessions
                                )
                            )
                        elif isinstance(declared_hashes, (list, tuple)):
                            declared_matches = list(declared_hashes) == sorted(hash_values)
                        elif (
                            declared_hashes is None
                            and len(snapshots) == 1
                            and isinstance(expected_universe, str)
                        ):
                            # The manifest may carry the sole PIT snapshot
                            # hash directly in ``versions.universe`` instead
                            # of repeating it under dataset attributes.
                            declared_matches = (
                                expected_universe == snapshots[0].content_hash
                            )
                        checks["universe_binding_matches"] = (
                            exact_sessions and version_matches and declared_matches
                        )
                    except Exception:
                        checks["universe_binding_matches"] = False
                else:
                    checks["universe_binding_matches"] = False
        checks["accepted_quality"] = lineage.checks.get("accepted_quality", False)
        checks["availability_attested"] = lineage.checks.get("availability_attested", False)
        checks["session_dates_valid"] = lineage.checks.get("session_dates_valid", False)
        checks["unique_revision_keys"] = lineage.checks.get("unique_revision_keys", False)
        checks["input_hash_matches"] = lineage.checks.get("input_hash_matches", False)
        checks["source_matches"] = lineage.checks.get("source_matches", False)
        checks["record_count_matches"] = lineage.checks.get("record_count_matches", False)

        # A result may be numerically unchanged by a different configuration
        # (for example when no trades occur). Hash equality alone therefore
        # cannot attest that the manifest's parameters were actually replayed.
        params = _strategy_parameters(strategy.get("parameters", {}))
        override = _strategy_parameters(strategy_config) if strategy_config is not None else params
        checks["strategy_configuration_bound"] = (
            params is not None and override is not None
            and hash_canonical(params, namespace="replay-strategy-parameters-v1")
            == hash_canonical(override, namespace="replay-strategy-parameters-v1")
        )
        # Delayed imports keep the research package usable while
        # backtesting/storage modules are still being initialized.
        from dataclasses import fields
        from quant_monitor.backtesting import BacktestConfig, BacktestEngine
        from quant_monitor.serialization import backtest_result_dict
        from quant_monitor.strategies import BankDailyMultifactorStrategy

        config_data = manifest.get("configuration", {})
        config_fields = {item.name for item in fields(BacktestConfig) if item.init}
        checks["execution_configuration_supported"] = (
            isinstance(config_data, Mapping) and set(config_data).issubset(config_fields)
        )
        if all(checks[name] for name in (
            "manifest_integrity", "supported_strategy", "strategy_code_version_matches", "result_integrity",
            "strategy_matches_manifest", "strategy_configuration_bound",
            "execution_configuration_supported", "successful_collection_batches",
            "batch_item_count_matches",
            "accepted_quality", "availability_attested", "unique_revision_keys",
            "session_dates_valid", "input_hash_matches", "source_matches", "interval_matches",
            "record_count_matches",
            "batch_provider_matches", "calendar_binding_matches",
            "response_receipt_consistent",
            "universe_binding_matches",
        )):
            # The optional compatibility argument is only an equality check;
            # execution always uses the parameters bound by the manifest.
            kwargs = dict(params)
            kwargs["strategy_name"] = _STRATEGY
            strategy_obj = BankDailyMultifactorStrategy(**kwargs)
            config_kwargs = dict(config_data)
            for key in ("daily_evaluation_time", "daily_execution_time"):
                value = config_kwargs.get(key)
                if isinstance(value, str):
                    config_kwargs[key] = time.fromisoformat(value)
            engine_config = BacktestConfig(**config_kwargs)
            bars_by_symbol = {}
            for item in observations:
                bars_by_symbol.setdefault(item.symbol, []).append(item.to_legacy_bar())
            replayed = BacktestEngine(strategy_obj, engine_config).run(
                bars_by_symbol,
                point_in_time_universe=point_in_time_universe,
                universe_knowledge_cutoff=universe_knowledge_cutoff,
            )
            replay_payload = backtest_result_dict(replayed)
            replay_hash = replay_payload.get("result_hash")
            checks["replay_result_hash_matches"] = replay_hash == expected_hash
            checks["result_hash_matches"] = checks["replay_result_hash_matches"]
    except Exception as exc:  # strict fail-closed: malformed evidence is never a pass
        error = type(exc).__name__
    return ReplayAuditReport(checks, expected_result_hash=expected_hash,
                             replayed_result_hash=replay_hash, error=error)


audit_replay = verify_replay

__all__ = ["ReplayAuditReport", "verify_replay", "audit_replay"]
