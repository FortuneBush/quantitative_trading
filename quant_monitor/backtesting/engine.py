"""Pure daily-bar backtest engine for research signals.

The module performs no I/O and has no broker concepts.  A ``Signal`` is
evaluated after a completed daily bar and is first attempted at the next
session's opening price.  An unavailable fill expires after its bounded
``valid_sessions`` window; it is never silently carried indefinitely.  Nothing
is sent to a broker.
"""

from __future__ import annotations

import math
import statistics
import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from quant_monitor.domain import (
    Adjustment,
    AdjustmentFactor,
    Bar,
    Instrument,
    SHANGHAI_TZ,
    Signal,
    SignalAction,
    ensure_market_time,
    normalize_symbol,
)
from quant_monitor.strategies import (
    CohortStrategy,
    CohortStrategyContext,
    Strategy,
    StrategyContext,
)
from quant_monitor.trading_calendar import (
    CalendarMetadata,
    DEFAULT_TRADING_CALENDAR,
    TradingCalendar,
)

from .models import (
    BacktestConfig,
    BacktestDiagnostics,
    BacktestMetrics,
    BacktestResult,
    EquityPoint,
    Position,
    SkippedExecution,
    Trade,
    TradingConstraint,
)


class BacktestValidationError(ValueError):
    """Raised when market data or strategy output breaks replay invariants."""


@dataclass
class _Lot:
    acquired_on: date
    quantity: int
    total_cost: Decimal


@dataclass
class _Holding:
    symbol: str
    lots: List[_Lot] = field(default_factory=list)
    last_price: Decimal = Decimal("0")

    @property
    def quantity(self) -> int:
        return sum(item.quantity for item in self.lots)

    @property
    def total_cost(self) -> Decimal:
        return sum((item.total_cost for item in self.lots), Decimal("0"))

    def settled_quantity(self, session: date) -> int:
        return sum(item.quantity for item in self.lots if item.acquired_on < session)

    def remove_settled(self, quantity: int, session: date) -> Decimal:
        remaining = int(quantity)
        removed_cost = Decimal("0")
        retained: List[_Lot] = []
        for item in self.lots:
            if remaining <= 0 or item.acquired_on >= session:
                retained.append(item)
                continue
            removed = min(remaining, item.quantity)
            per_share = item.total_cost / Decimal(item.quantity)
            removed_cost += per_share * Decimal(removed)
            left = item.quantity - removed
            if left:
                retained.append(
                    _Lot(
                        acquired_on=item.acquired_on,
                        quantity=left,
                        total_cost=per_share * Decimal(left),
                    )
                )
            remaining -= removed
        if remaining:
            raise BacktestValidationError("attempted to sell unsettled quantity")
        self.lots = retained
        return removed_cost


@dataclass(frozen=True)
class _PendingSignal:
    signal: Signal
    attempts_remaining: int


class BacktestEngine:
    """Replay a single ``Strategy`` or ``CohortStrategy`` on daily bars.

    BUY opens one position and SELL liquidates all T+1-settled shares.  When
    several symbols emit BUY on the same date, available cash is split equally
    in normalized-symbol order, subject to ``max_position_weight``.  Existing
    positions are not pyramided.  These rules make signal-to-portfolio mapping
    explicit and reproducible while keeping strategies themselves stateless.
    """

    def __init__(
        self,
        strategy: Union[Strategy, CohortStrategy],
        config: Optional[BacktestConfig] = None,
        *,
        instruments: Optional[Mapping[str, Instrument]] = None,
    ) -> None:
        if not isinstance(strategy, (Strategy, CohortStrategy)):
            raise TypeError("strategy must implement Strategy or CohortStrategy")
        self.strategy = strategy
        self.config = config or BacktestConfig()
        normalized_instruments: Dict[str, Instrument] = {}
        for raw_symbol, instrument in dict(instruments or {}).items():
            symbol = normalize_symbol(raw_symbol)
            if not isinstance(instrument, Instrument):
                raise TypeError("instruments values must be Instrument instances")
            if instrument.symbol != symbol:
                raise ValueError("instrument key does not match instrument symbol")
            if symbol in normalized_instruments:
                raise ValueError("duplicate instrument symbol: {}".format(symbol))
            normalized_instruments[symbol] = instrument
        self._instruments = normalized_instruments

    def run(
        self,
        bars_by_symbol: Mapping[str, Sequence[Bar]],
        *,
        trading_constraints: Iterable[TradingConstraint] = (),
        session_calendar: Optional[Sequence[Union[date, datetime]]] = None,
        trading_calendar: Optional[TradingCalendar] = None,
        point_in_time_universe: Optional[object] = None,
        universe_knowledge_cutoff: Optional[datetime] = None,
        fundamental_factor_reports: Optional[
            Mapping[Union[date, str], Mapping[str, object]]
        ] = None,
        adjustment_factors: Optional[Mapping[Tuple[str, date], AdjustmentFactor]] = None,
        cancellation_check: Optional[Callable[[], None]] = None,
    ) -> BacktestResult:
        """Run the deterministic replay.

        ``point_in_time_universe`` is an explicit research-only binding.  If
        supplied it must be a :class:`UniverseSnapshot` (for a single-session
        replay), a ``{session_date: UniverseSnapshot}`` mapping loaded from
        immutable research storage, or a :class:`PointInTimeUniverse`
        together with an explicit ``universe_knowledge_cutoff``.  Every
        supplied bar is checked against the members effective on its own
        session; bars for a future listing or a known delisted member fail
        closed instead of being silently used.
        The argument is optional for backwards compatibility with ordinary
        exploratory backtests, but strict research callers should always
        provide it.

        ``cancellation_check`` is an optional cooperative lifecycle hook. It
        may raise to stop before preparation, at session boundaries and after
        strategy evaluation. It is never supplied to a strategy and does not
        change any successful replay's financial output.
        """
        if cancellation_check is not None and not callable(cancellation_check):
            raise TypeError("cancellation_check must be callable")
        checkpoint = cancellation_check if cancellation_check is not None else (lambda: None)
        checkpoint()
        factors = self._prepare_adjustment_factors(adjustment_factors)
        # Keep the raw, unadjusted observations as the authoritative execution
        # and valuation stream.  An adjusted projection is built separately
        # for strategy context only; otherwise a split/dividend factor would
        # change simulated fills, cash, position sizing and NAV.
        bars, observed_sessions, strategy_bars = self._prepare_bars(
            bars_by_symbol, factors
        )
        universe_binding = self._validate_point_in_time_universe(
            bars,
            observed_sessions,
            point_in_time_universe,
            universe_knowledge_cutoff,
            decision_wall_time=self.config.daily_evaluation_time,
        )
        calendar_fingerprint = (
            getattr(trading_calendar, "fingerprint", None)
            if trading_calendar is not None
            else None
        )
        sessions, calendar_metadata = self._prepare_calendar(
            session_calendar, observed_sessions, trading_calendar
        )
        normalized_fundamental_reports = self._prepare_fundamental_reports(
            fundamental_factor_reports,
            sessions,
            evaluation_time=self.config.daily_evaluation_time,
        )
        constraints = self._prepare_constraints(trading_constraints)
        self._validate_constraint_scope(constraints, bars, sessions)
        cash = self.config.initial_cash
        holdings: Dict[str, _Holding] = {}
        last_prices: Dict[str, Decimal] = {}
        trades: List[Trade] = []
        skipped: List[SkippedExecution] = []
        equity: List[EquityPoint] = []
        pending: Tuple[_PendingSignal, ...] = ()
        seen_signal_keys: Set[Tuple[str, str, str, object]] = set()

        for session in sessions:
            checkpoint()
            current = {
                symbol: symbol_bars[session]
                for symbol, symbol_bars in bars.items()
                if session in symbol_bars
            }
            execution_timestamp = self._session_timestamp(
                session, self.config.daily_execution_time
            )
            cash, retry_pending = self._execute_pending(
                pending,
                session,
                execution_timestamp,
                current,
                constraints,
                cash,
                holdings,
                last_prices,
                trades,
                skipped,
            )
            for symbol, bar in current.items():
                last_prices[symbol] = bar.close
                if symbol in holdings:
                    holdings[symbol].last_price = bar.close
            # A held position must have a valuation observation on every
            # equity session.  Reusing the previous close when a symbol has
            # no bar would silently create a stale mark-to-market value and
            # understate both return and risk.  The research contract is
            # fail-closed: callers must provide an explicit valuation bar (or
            # end the holding before the gap) rather than letting an old
            # price drift through the equity curve.
            missing_valuation_symbols = sorted(
                symbol
                for symbol, holding in holdings.items()
                if holding.quantity > 0 and symbol not in current
            )
            if missing_valuation_symbols:
                raise BacktestValidationError(
                    "missing valuation bar for held symbol(s) on {}: {}".format(
                        session.isoformat(), ", ".join(missing_valuation_symbols)
                    )
                )
            evaluation_timestamp = self._session_timestamp(
                session, self.config.daily_evaluation_time
            )
            equity.append(self._equity_point(evaluation_timestamp, cash, holdings, equity))
            strategy_current = {
                symbol: symbol_bars[session]
                for symbol, symbol_bars in strategy_bars.items()
                if session in symbol_bars
            }
            emitted = self._evaluate(
                session,
                evaluation_timestamp,
                strategy_bars,
                strategy_current,
                fundamental_factor_report=normalized_fundamental_reports.get(session),
            )
            checkpoint()
            new_pending = self._validate_signals(
                emitted, evaluation_timestamp, seen_signal_keys
            )
            replacement_symbols = {item.signal.symbol for item in new_pending}
            pending = tuple(
                item
                for item in retry_pending
                if item.signal.symbol not in replacement_symbols
            ) + tuple(new_pending)

        checkpoint()
        for item in pending:
            skipped.append(
                SkippedExecution(
                    signal=item.signal,
                    attempted_at=None,
                    reason="no_next_session",
                    detail="signal validity extended beyond the available session calendar",
                    will_retry=False,
                )
            )

        positions = self._positions(holdings, sessions[-1])
        metrics = self._metrics(equity, trades)
        diagnostics = self._diagnostics(
            calendar_metadata=calendar_metadata,
            bars=bars,
            sessions=sessions,
            constraints=constraints,
            trades=trades,
            skipped=skipped,
            universe_binding=universe_binding,
            adjustment_binding=self._adjustment_binding(factors),
            calendar_fingerprint=calendar_fingerprint,
            fundamental_binding=self._fundamental_binding(
                normalized_fundamental_reports, sessions
            ),
        )
        return BacktestResult(
            strategy_name=self.strategy.name,
            config=self.config,
            started_at=equity[0].timestamp,
            ended_at=equity[-1].timestamp,
            equity_curve=tuple(equity),
            trades=tuple(trades),
            skipped_executions=tuple(skipped),
            positions=positions,
            metrics=metrics,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _validate_point_in_time_universe(
        bars: Mapping[str, Mapping[date, Bar]],
        observed_sessions: Sequence[date],
        point_in_time_universe: Optional[object],
        knowledge_cutoff: Optional[datetime],
        *,
        decision_wall_time: time = time(15, 30),
    ) -> Optional[Mapping[str, object]]:
        """Validate and describe a supplied PIT universe without I/O.

        Imports are intentionally local: the research package imports the
        backtest engine for independent replay, so importing its domain types
        at module import time would create a cycle.  An arbitrary duck-typed
        object is not accepted at this security boundary.
        """
        if point_in_time_universe is None:
            if knowledge_cutoff is not None:
                raise BacktestValidationError(
                    "universe_knowledge_cutoff requires point_in_time_universe"
                )
            return None
        from quant_monitor.research.universe import PointInTimeUniverse, UniverseSnapshot

        if isinstance(point_in_time_universe, UniverseSnapshot):
            if knowledge_cutoff is not None:
                raise BacktestValidationError(
                    "universe_knowledge_cutoff is not used with a UniverseSnapshot"
                )
            if len(set(observed_sessions)) != 1 or observed_sessions[0] != point_in_time_universe.session_date:
                raise BacktestValidationError(
                    "a UniverseSnapshot can only bind bars from its single session"
                )
            by_session = {point_in_time_universe.session_date: point_in_time_universe}
        elif isinstance(point_in_time_universe, PointInTimeUniverse):
            if knowledge_cutoff is None:
                raise BacktestValidationError(
                    "universe_knowledge_cutoff is required for PointInTimeUniverse"
                )
            # A single cutoff is shared by every session in this replay.  It
            # must not be later than the first decision timestamp: otherwise
            # a classification/listing revision learned after the historical
            # trade could leak into the earlier universe.  The resolver itself
            # intentionally supports arbitrary as-of queries for research
            # inspection; this stricter guard belongs at the backtest boundary
            # where the decision-time semantics are known.
            normalized_cutoff = ensure_market_time(knowledge_cutoff)
            if not isinstance(decision_wall_time, time):
                raise TypeError("decision_wall_time must be a datetime.time")
            if decision_wall_time.tzinfo is not None and decision_wall_time.utcoffset() is not None:
                raise ValueError("decision_wall_time must be timezone-naive Shanghai wall time")
            if observed_sessions:
                earliest_decision = datetime.combine(
                    min(observed_sessions),
                    decision_wall_time.replace(tzinfo=None),
                    tzinfo=SHANGHAI_TZ,
                )
                if normalized_cutoff > earliest_decision:
                    raise BacktestValidationError(
                        "universe_knowledge_cutoff cannot be after the earliest "
                        "backtest decision timestamp"
                    )
            by_session = {}
            try:
                for session in observed_sessions:
                    members = point_in_time_universe.members_at(session, knowledge_cutoff)
                    symbols = tuple(item.symbol for item in members)
                    if len(set(symbols)) != len(symbols):
                        raise BacktestValidationError(
                            "point-in-time universe contains duplicate symbols"
                        )
                    # Resolve a content-addressed snapshot for the exact
                    # session/cutoff.  This also checks temporal eligibility.
                    by_session[session] = point_in_time_universe.snapshot_from_members(
                        session, knowledge_cutoff, members
                    )
            except BacktestValidationError:
                raise
            except Exception as exc:
                raise BacktestValidationError(
                    "point-in-time universe resolution failed: {}".format(type(exc).__name__)
                ) from None
        elif isinstance(point_in_time_universe, Mapping):
            # A persisted repository cannot safely be reconstructed as a
            # resolver: doing so could apply one revision to every session.
            # Require an explicit, immutable snapshot for each observed
            # session and validate the mapping key against the snapshot's own
            # session date.  ``universe_knowledge_cutoff`` is intentionally
            # rejected because each persisted row already carries its own
            # cutoff; silently overriding it would detach the evidence.
            if knowledge_cutoff is not None:
                raise BacktestValidationError(
                    "universe_knowledge_cutoff is not used with persisted snapshot mappings"
                )
            by_session = {}
            for raw_session, snapshot in point_in_time_universe.items():
                if isinstance(raw_session, datetime):
                    raise BacktestValidationError("persisted universe snapshot session key is invalid")
                if isinstance(raw_session, date):
                    session = raw_session
                elif isinstance(raw_session, str):
                    try:
                        session = date.fromisoformat(raw_session)
                    except (TypeError, ValueError):
                        raise BacktestValidationError(
                            "persisted universe snapshot session key is invalid"
                        ) from None
                else:
                    raise BacktestValidationError(
                        "persisted universe snapshot session key is invalid"
                    )
                if not isinstance(snapshot, UniverseSnapshot) or snapshot.session_date != session:
                    raise BacktestValidationError(
                        "persisted universe snapshot does not match its session key"
                    )
                if not isinstance(decision_wall_time, time):
                    raise TypeError("decision_wall_time must be a datetime.time")
                if decision_wall_time.tzinfo is not None and decision_wall_time.utcoffset() is not None:
                    raise ValueError("decision_wall_time must be a timezone-naive Shanghai wall time")
                decision_at = datetime.combine(
                    session,
                    decision_wall_time.replace(tzinfo=None),
                    tzinfo=SHANGHAI_TZ,
                )
                if snapshot.knowledge_cutoff > decision_at:
                    raise BacktestValidationError(
                        "persisted universe snapshot knowledge_cutoff cannot be after "
                        "the session decision timestamp"
                    )
                prior = by_session.get(session)
                if prior is not None and prior.content_hash != snapshot.content_hash:
                    raise BacktestValidationError(
                        "conflicting persisted universe snapshots for {}".format(session.isoformat())
                    )
                by_session[session] = snapshot
            if set(by_session) != set(observed_sessions):
                raise BacktestValidationError(
                    "persisted universe snapshots must cover every observed session"
                )
        else:
            raise BacktestValidationError(
                "point_in_time_universe must be a UniverseSnapshot, snapshot mapping, or PointInTimeUniverse"
            )

        for symbol, symbol_bars in bars.items():
            for session in symbol_bars:
                snapshot = by_session.get(session)
                if snapshot is None or symbol not in snapshot.symbols:
                    raise BacktestValidationError(
                        "bar symbol {} is outside point-in-time universe on {}".format(
                            symbol, session.isoformat()
                        )
                    )
        versions = {snapshot.version for snapshot in by_session.values()}
        hashes = {snapshot.content_hash for snapshot in by_session.values()}
        # Keep the result hash bound to the exact resolved snapshots.  The
        # complete per-session mapping is deterministic and contains no
        # credentials or provider payloads.
        cutoffs = {snapshot.knowledge_cutoff.isoformat() for snapshot in by_session.values()}
        return {
            "mode": "point_in_time",
            "version": next(iter(versions)) if len(versions) == 1 else "mixed",
            "snapshot_hashes": tuple(sorted(hashes)),
            "knowledge_cutoff": (
                knowledge_cutoff.isoformat() if knowledge_cutoff is not None
                else next(iter(cutoffs)) if len(cutoffs) == 1 else "mixed"
            ),
        }

    def _prepare_bars(
        self,
        bars_by_symbol: Mapping[str, Sequence[Bar]],
        adjustment_factors: Mapping[Tuple[str, date], AdjustmentFactor],
    ) -> Tuple[
        Dict[str, Dict[date, Bar]],
        Tuple[date, ...],
        Dict[str, Dict[date, Bar]],
    ]:
        normalized: Dict[str, Dict[date, Bar]] = {}
        strategy_projection: Dict[str, Dict[date, Bar]] = {}
        all_sessions: Set[date] = set()
        for raw_symbol, raw_bars in dict(bars_by_symbol or {}).items():
            symbol = normalize_symbol(raw_symbol)
            if symbol in normalized:
                raise BacktestValidationError("duplicate normalized symbol: {}".format(symbol))
            by_session: Dict[date, Bar] = {}
            strategy_by_session: Dict[date, Bar] = {}
            previous_timestamp: Optional[datetime] = None
            for bar in raw_bars:
                if not isinstance(bar, Bar):
                    raise TypeError("bars must be Bar instances")
                if bar.symbol != symbol:
                    raise BacktestValidationError("bar symbol does not match mapping key")
                if bar.interval != self.config.interval:
                    raise BacktestValidationError(
                        "bar interval must be {}".format(self.config.interval)
                    )
                if previous_timestamp is not None and bar.timestamp < previous_timestamp:
                    raise BacktestValidationError(
                        "bars for {} must be strictly increasing".format(symbol)
                    )
                previous_timestamp = bar.timestamp
                session = bar.timestamp.date()
                available_at = self._session_timestamp(
                    session, self.config.daily_evaluation_time
                )
                if bar.timestamp > available_at:
                    raise BacktestValidationError(
                        "daily bar timestamp for {} is after daily_evaluation_time".format(
                            symbol
                        )
                    )
                if session in by_session:
                    raise BacktestValidationError(
                        "multiple daily bars for {} on {}".format(symbol, session.isoformat())
                    )
                basis = self._price_basis_adjustment()
                source_parts = bar.source.rsplit("|", 2)
                marked_adjusted = len(source_parts) == 3 and source_parts[1] in {
                    item.value for item in Adjustment
                }
                strategy_bar = bar
                if basis == Adjustment.RAW:
                    if adjustment_factors:
                        raise BacktestValidationError(
                            "adjustment_factors cannot be supplied for raw_unadjusted backtests"
                        )
                    if marked_adjusted:
                        raise BacktestValidationError(
                            "raw_unadjusted backtest received a materialized adjusted bar"
                        )
                else:
                    factor = adjustment_factors.get((symbol, session))
                    if factor is None:
                        raise BacktestValidationError(
                            "{} backtest requires an adjustment factor for {} on {}".format(
                                self.config.price_basis, symbol, session.isoformat()
                            )
                        )
                    if factor.adjustment != basis:
                        raise BacktestValidationError(
                            "adjustment factor basis does not match backtest price_basis"
                        )
                    if marked_adjusted:
                        raise BacktestValidationError(
                            "adjusted backtest received a materialized adjusted input bar"
                        )
                    strategy_bar = factor.apply_bar(bar).to_bar()
                by_session[session] = bar
                strategy_by_session[session] = strategy_bar
                all_sessions.add(session)
            normalized[symbol] = by_session
            strategy_projection[symbol] = strategy_by_session
        if not all_sessions:
            raise BacktestValidationError("at least one daily bar is required")
        if adjustment_factors:
            expected_factor_keys = {
                (symbol, session)
                for symbol, symbol_bars in normalized.items()
                for session in symbol_bars
            }
            extra_factor_keys = set(adjustment_factors) - expected_factor_keys
            if extra_factor_keys:
                symbol, session = sorted(extra_factor_keys, key=lambda item: (item[0], item[1]))[0]
                raise BacktestValidationError(
                    "adjustment factor {} on {} is outside the supplied bars".format(
                        symbol, session.isoformat()
                    )
                )
        return normalized, tuple(sorted(all_sessions)), strategy_projection

    def _price_basis_adjustment(self) -> Adjustment:
        value = self.config.price_basis.strip().lower()
        if value == "raw_unadjusted" or value == Adjustment.RAW.value:
            return Adjustment.RAW
        if value == Adjustment.QFQ.value or value.startswith("qfq_"):
            return Adjustment.QFQ
        if value == Adjustment.HFQ.value or value.startswith("hfq_"):
            return Adjustment.HFQ
        raise BacktestValidationError(
            "unsupported price_basis {}; use raw_unadjusted, qfq or hfq".format(
                self.config.price_basis
            )
        )

    @staticmethod
    def _prepare_adjustment_factors(
        values: Optional[Mapping[Tuple[str, date], AdjustmentFactor]],
    ) -> Dict[Tuple[str, date], AdjustmentFactor]:
        if values is None:
            return {}
        result: Dict[Tuple[str, date], AdjustmentFactor] = {}
        for raw_key, factor in dict(values).items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                raise BacktestValidationError(
                    "adjustment factor keys must be (symbol, session_date) tuples"
                )
            symbol = normalize_symbol(raw_key[0])
            session = raw_key[1]
            if not isinstance(session, date) or isinstance(session, datetime):
                raise BacktestValidationError("adjustment factor key date is invalid")
            if not isinstance(factor, AdjustmentFactor):
                raise TypeError("adjustment_factors values must be AdjustmentFactor instances")
            if factor.symbol != symbol or factor.session_date != session:
                raise BacktestValidationError(
                    "adjustment factor key does not match factor identity"
                )
            key = (symbol, session)
            if key in result:
                raise BacktestValidationError("duplicate adjustment factor")
            result[key] = factor
        return result

    @staticmethod
    def _prepare_fundamental_reports(
        reports: Optional[Mapping[Union[date, str], Mapping[str, object]]],
        sessions: Sequence[date],
        *,
        evaluation_time: time,
    ) -> Dict[date, Mapping[str, object]]:
        """Validate caller-supplied PIT factor reports for replay.

        The backtest kernel deliberately does not query SQLite or a provider.
        A research caller may nevertheless bind one already materialised
        factor report to each session.  Keys are explicit session dates and
        reports are copied before entering strategy metadata so a strategy
        cannot mutate the caller's evidence.  Completeness and metric-level
        validation remain the responsibility of the strategy's pure overlay
        contract; this boundary enforces only temporal and shape invariants.
        """
        if reports is None:
            return {}
        if not isinstance(reports, Mapping):
            raise TypeError("fundamental_factor_reports must be a mapping")
        session_set = set(sessions)
        normalized: Dict[date, Mapping[str, object]] = {}
        for raw_session, raw_report in reports.items():
            if isinstance(raw_session, datetime):
                raise BacktestValidationError(
                    "fundamental factor report session key must be a date"
                )
            if isinstance(raw_session, date):
                session = raw_session
            elif isinstance(raw_session, str):
                try:
                    session = date.fromisoformat(raw_session)
                except (TypeError, ValueError):
                    raise BacktestValidationError(
                        "fundamental factor report session key is invalid"
                    ) from None
            else:
                raise BacktestValidationError(
                    "fundamental factor report session key is invalid"
                )
            if session not in session_set:
                raise BacktestValidationError(
                    "fundamental factor report session is outside backtest calendar: {}".format(
                        session.isoformat()
                    )
                )
            if session in normalized:
                raise BacktestValidationError(
                    "duplicate fundamental factor report session: {}".format(
                        session.isoformat()
                    )
                )
            if not isinstance(raw_report, Mapping):
                raise BacktestValidationError(
                    "fundamental factor report must be an object"
                )
            if raw_report.get("selection") != "bank_fundamental_factors_as_of_v1":
                raise BacktestValidationError(
                    "fundamental factor report selection is unsupported"
                )
            try:
                decision_at = ensure_market_time(
                    datetime.fromisoformat(str(raw_report["decision_at"]))
                )
            except (KeyError, TypeError, ValueError):
                raise BacktestValidationError(
                    "fundamental factor report decision_at is invalid"
                ) from None
            decision_timestamp = datetime.combine(
                session,
                evaluation_time.replace(tzinfo=None),
                tzinfo=SHANGHAI_TZ,
            )
            if decision_at > decision_timestamp:
                raise BacktestValidationError(
                    "fundamental factor report is after its backtest decision time"
                )
            # A detached nested list/dict could otherwise be changed after
            # validation and alter future strategy decisions.  deepcopy keeps
            # the pure strategy boundary independent of the caller object.
            try:
                detached = copy.deepcopy(dict(raw_report))
            except Exception as exc:
                raise BacktestValidationError(
                    "fundamental factor report is not safely copyable: {}".format(
                        type(exc).__name__
                    )
                ) from None
            try:
                # Keep a content address in the result diagnostics.  The
                # digest is deliberately computed over canonical JSON only;
                # it is an integrity locator, not a claim that the source is
                # independently authenticated.
                BacktestEngine._fundamental_report_hash(detached)
            except (TypeError, ValueError, OverflowError):
                raise BacktestValidationError(
                    "fundamental factor report must be JSON-serializable"
                ) from None
            normalized[session] = detached
        return normalized

    @staticmethod
    def _fundamental_report_hash(report: Mapping[str, object]) -> str:
        try:
            encoded = json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            raise
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _fundamental_binding(
        reports: Mapping[date, Mapping[str, object]], sessions: Sequence[date]
    ) -> Mapping[str, object]:
        if not reports:
            return {"status": "not_bound", "report_count": 0, "session_count": len(sessions)}
        provided = tuple(sorted(item.isoformat() for item in reports))
        missing = tuple(
            sorted(item.isoformat() for item in sessions if item not in reports)
        )
        return {
            "status": "bound_unattested",
            "report_count": len(reports),
            "session_count": len(sessions),
            "provided_sessions": provided,
            "missing_sessions": missing,
            "complete": not missing,
            "hash_algorithm": "sha256-json-v1",
            "report_hashes": {
                item.isoformat(): BacktestEngine._fundamental_report_hash(reports[item])
                for item in sorted(reports)
            },
        }

    @staticmethod
    def _adjustment_binding(
        factors: Mapping[Tuple[str, date], AdjustmentFactor],
    ) -> Mapping[str, object]:
        if not factors:
            return {"status": "not_bound", "factor_count": 0, "factor_versions": ()}
        versions = tuple(sorted({item.factor_version for item in factors.values()}))
        revisions = tuple(sorted({item.revision_id for item in factors.values()}))
        return {
            "status": "bound_unattested",
            "factor_count": len(factors),
            "factor_versions": versions,
            "revision_ids": revisions,
            "factor_ids": tuple(sorted(item.factor_id for item in factors.values())),
        }

    @staticmethod
    def _prepare_calendar(
        calendar: Optional[Sequence[Union[date, datetime]]],
        observed_sessions: Tuple[date, ...],
        trading_calendar: Optional[TradingCalendar],
    ) -> Tuple[Tuple[date, ...], CalendarMetadata]:
        if calendar is not None and trading_calendar is not None:
            raise BacktestValidationError(
                "session_calendar and trading_calendar are mutually exclusive"
            )
        if trading_calendar is not None:
            coverage = trading_calendar.coverage
            if coverage is not None:
                if not isinstance(coverage, tuple) or len(coverage) != 2:
                    raise BacktestValidationError(
                        "trading_calendar coverage must be a two-date tuple or None"
                    )
                first, last = coverage
                if (
                    isinstance(first, datetime)
                    or isinstance(last, datetime)
                    or not isinstance(first, date)
                    or not isinstance(last, date)
                    or first > last
                ):
                    raise BacktestValidationError(
                        "trading_calendar coverage must contain ordered dates"
                    )
                if observed_sessions[0] < first or observed_sessions[-1] > last:
                    raise BacktestValidationError(
                        "observed bar range is outside trading_calendar coverage"
                    )
            sessions = tuple(
                trading_calendar.sessions_between(
                    observed_sessions[0], observed_sessions[-1]
                )
            )
            if not sessions:
                raise BacktestValidationError("trading_calendar contains no sessions")
            if any(
                isinstance(item, datetime) or not isinstance(item, date)
                for item in sessions
            ):
                raise BacktestValidationError(
                    "trading_calendar sessions must be dates"
                )
            if any(left >= right for left, right in zip(sessions, sessions[1:])):
                raise BacktestValidationError(
                    "trading_calendar sessions must be strictly increasing and unique"
                )
            if (
                sessions[0] < observed_sessions[0]
                or sessions[-1] > observed_sessions[-1]
            ):
                raise BacktestValidationError(
                    "trading_calendar returned sessions outside the requested range"
                )
            session_set = set(sessions)
            # A caller may include leading history bars solely to warm up a
            # stateful/rolling strategy.  They are context, never execution
            # sessions.  Bars after the requested range remain a future-data
            # violation and are rejected.
            missing = [
                item
                for item in observed_sessions
                if item not in session_set and item >= sessions[0]
            ]
            if missing:
                raise BacktestValidationError(
                    "trading_calendar omits observed bar session {}".format(
                        missing[0].isoformat()
                    )
                )
            try:
                supplied_metadata = trading_calendar.metadata
                metadata = CalendarMetadata(
                    identifier=supplied_metadata.identifier,
                    source=supplied_metadata.source,
                    version=supplied_metadata.version,
                    notice=supplied_metadata.notice,
                )
            except (AttributeError, TypeError, ValueError):
                raise BacktestValidationError(
                    "trading_calendar metadata is invalid"
                ) from None
            return sessions, metadata
        if calendar is None:
            # Never infer an exchange session from an observed weekend row.
            # Callers with an exchange-specific makeup session or holiday
            # schedule must supply an explicit TradingCalendar snapshot.
            off_weekday = [
                item for item in observed_sessions
                if not DEFAULT_TRADING_CALENDAR.is_session(item)
            ]
            if off_weekday:
                raise BacktestValidationError(
                    "observed bar session {} is not a weekday session; ".format(
                        off_weekday[0].isoformat()
                    )
                    + "supply an explicit trading_calendar for exchange exceptions"
                )
            metadata = DEFAULT_TRADING_CALENDAR.metadata
            return observed_sessions, CalendarMetadata(
                identifier="observed_sessions_fallback",
                source=metadata.source,
                version=metadata.version,
                notice=metadata.notice,
            )
        sessions: List[date] = []
        for raw_value in calendar:
            if isinstance(raw_value, datetime):
                if raw_value.tzinfo is None or raw_value.utcoffset() is None:
                    raise BacktestValidationError(
                        "datetime session_calendar values must be timezone-aware"
                    )
                value = raw_value.astimezone(SHANGHAI_TZ).date()
            elif isinstance(raw_value, date):
                value = raw_value
            else:
                raise TypeError("session_calendar values must be dates or datetimes")
            sessions.append(value)
        if not sessions:
            raise BacktestValidationError("session_calendar cannot be empty")
        if any(left >= right for left, right in zip(sessions, sessions[1:])):
            raise BacktestValidationError(
                "session_calendar must be strictly increasing and unique"
            )
        session_set = set(sessions)
        missing = [
            item
            for item in observed_sessions
            if item not in session_set and item >= sessions[0]
        ]
        if missing:
            raise BacktestValidationError(
                "session_calendar omits observed bar session {}".format(missing[0].isoformat())
            )
        return tuple(sessions), CalendarMetadata(
            identifier="explicit_session_calendar",
            source="caller_supplied_dates",
            version="unversioned",
            notice=(
                "Caller-supplied session dates without authenticated provenance; "
                "not an official exchange calendar."
            ),
        )

    @staticmethod
    def _session_timestamp(session: date, wall_time) -> datetime:
        return datetime.combine(session, wall_time, tzinfo=SHANGHAI_TZ)

    @staticmethod
    def _prepare_constraints(
        values: Iterable[TradingConstraint],
    ) -> Dict[Tuple[str, date], TradingConstraint]:
        result: Dict[Tuple[str, date], TradingConstraint] = {}
        for value in values:
            if not isinstance(value, TradingConstraint):
                raise TypeError("trading_constraints must contain TradingConstraint instances")
            key = (value.symbol, value.session_date)
            if key in result:
                raise BacktestValidationError(
                    "duplicate trading constraint for {} on {}".format(
                        value.symbol, value.session_date.isoformat()
                    )
                )
            result[key] = value
        return result

    @staticmethod
    def _validate_constraint_scope(
        constraints: Mapping[Tuple[str, date], TradingConstraint],
        bars: Mapping[str, Mapping[date, Bar]],
        sessions: Sequence[date],
    ) -> None:
        session_set = set(sessions)
        for symbol, session in constraints:
            if symbol not in bars:
                raise BacktestValidationError(
                    "trading constraint symbol is outside bars_by_symbol: {}".format(symbol)
                )
            if session not in session_set:
                raise BacktestValidationError(
                    "trading constraint date is outside session_calendar: {}".format(
                        session.isoformat()
                    )
                )

    def _evaluate(
        self,
        session: date,
        timestamp: datetime,
        bars: Mapping[str, Mapping[date, Bar]],
        current: Mapping[str, Bar],
        *,
        fundamental_factor_report: Optional[Mapping[str, object]] = None,
    ) -> Sequence[Signal]:
        if isinstance(self.strategy, Strategy):
            signals: List[Signal] = []
            for symbol in sorted(current):
                history = tuple(bars[symbol][day] for day in sorted(bars[symbol]) if day <= session)
                context = StrategyContext(
                    symbol=symbol,
                    timestamp=timestamp,
                    bars=history,
                )
                produced = self.strategy.evaluate(context)
                if produced is None:
                    raise BacktestValidationError("strategy.evaluate() returned None")
                for signal in produced:
                    if signal.symbol != symbol:
                        raise BacktestValidationError(
                            "individual strategy emitted a signal for another symbol"
                        )
                    signals.append(signal)
            return tuple(signals)

        members = {}
        # Preserve the declared research universe when a security has no bar
        # today.  Its stale history remains visible to cohort quality gates
        # instead of creating survivorship-by-availability.
        for symbol in sorted(bars):
            history = tuple(bars[symbol][day] for day in sorted(bars[symbol]) if day <= session)
            if not history:
                continue
            members[symbol] = StrategyContext(
                symbol=symbol,
                timestamp=timestamp,
                bars=history,
            )
        context = CohortStrategyContext(
            timestamp=timestamp,
            members=members,
            cohort="backtest",
            metadata={
                "backtest": True,
                "expected_bar_date": session.isoformat(),
                "price_basis": self.config.price_basis,
                # This is an explicit, caller-supplied PIT report.  It is
                # absent when no report was bound, preserving price-only
                # behaviour for ordinary exploratory backtests.
                "fundamental_factor_report": fundamental_factor_report,
            },
        )
        produced = self.strategy.evaluate(context)
        if produced is None:
            raise BacktestValidationError("strategy.evaluate() returned None")
        return tuple(produced)

    def _validate_signals(
        self,
        signals: Sequence[Signal],
        evaluated_at: datetime,
        seen: Set[Tuple[str, str, str, object]],
    ) -> Tuple[_PendingSignal, ...]:
        selected: Dict[str, Signal] = {}
        for signal in signals:
            if not isinstance(signal, Signal):
                raise BacktestValidationError("strategy outputs must be Signal instances")
            if signal.strategy != self.strategy.name:
                raise BacktestValidationError("signal strategy does not match evaluated strategy")
            if signal.timestamp > evaluated_at:
                raise BacktestValidationError("strategy emitted a future-dated signal")
            if signal.timestamp.date() != evaluated_at.date():
                raise BacktestValidationError("strategy emitted a stale-dated signal")
            if signal.action == SignalAction.HOLD:
                continue
            # Daily providers commonly stamp a bar at 00:00 to identify its
            # trading date.  It is not available then.  Preserve the strategy
            # output content while normalizing the replay signal to the
            # explicit post-close evaluation point.
            signal = replace(signal, timestamp=evaluated_at)
            key = (signal.strategy, signal.symbol, signal.action.value, signal.timestamp)
            if key in seen:
                continue
            seen.add(key)
            if signal.symbol in selected:
                raise BacktestValidationError(
                    "strategy emitted multiple executable actions for {} in one session".format(
                        signal.symbol
                    )
                )
            selected[signal.symbol] = signal
        return tuple(
            _PendingSignal(
                signal=selected[symbol],
                attempts_remaining=self._signal_valid_sessions(selected[symbol]),
            )
            for symbol in sorted(selected)
        )

    def _signal_valid_sessions(self, signal: Signal) -> int:
        raw_value = signal.metadata.get(
            "valid_sessions", self.config.default_signal_valid_sessions
        )
        if isinstance(raw_value, bool):
            raise BacktestValidationError("signal valid_sessions must be an integer")
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            raise BacktestValidationError("signal valid_sessions must be an integer") from None
        try:
            if Decimal(str(raw_value)) != Decimal(value):
                raise BacktestValidationError("signal valid_sessions must be an integer")
        except (InvalidOperation, ValueError):
            raise BacktestValidationError("signal valid_sessions must be an integer") from None
        if value <= 0 or value > self.config.maximum_signal_valid_sessions:
            raise BacktestValidationError(
                "signal valid_sessions must be between 1 and {}".format(
                    self.config.maximum_signal_valid_sessions
                )
            )
        return value

    def _execute_pending(
        self,
        pending_signals: Sequence[_PendingSignal],
        session: date,
        timestamp: datetime,
        current: Mapping[str, Bar],
        constraints: Mapping[Tuple[str, date], TradingConstraint],
        cash: Decimal,
        holdings: Dict[str, _Holding],
        last_prices: Mapping[str, Decimal],
        trades: List[Trade],
        skipped: List[SkippedExecution],
    ) -> Tuple[Decimal, Tuple[_PendingSignal, ...]]:
        deferred: List[_PendingSignal] = []
        sells = [item for item in pending_signals if item.signal.action == SignalAction.SELL]
        buys = [item for item in pending_signals if item.signal.action == SignalAction.BUY]
        for item in sells:
            cash, retry = self._sell(
                item,
                session,
                timestamp,
                current,
                constraints,
                cash,
                holdings,
                trades,
                skipped,
            )
            if retry is not None:
                deferred.append(retry)

        eligible: List[Tuple[_PendingSignal, Bar]] = []
        for item in buys:
            signal = item.signal
            bar = current.get(signal.symbol)
            reason = self._untradeable_reason(signal, session, bar, constraints)
            if reason is not None:
                retry = self._record_market_skip(
                    item, timestamp, reason, constraints, skipped
                )
                if retry is not None:
                    deferred.append(retry)
            elif signal.symbol in holdings and holdings[signal.symbol].quantity > 0:
                skipped.append(
                    SkippedExecution(
                        signal, timestamp, "already_positioned", will_retry=False
                    )
                )
            else:
                eligible.append((item, bar))  # type: ignore[arg-type]

        nav_at_open = cash
        for symbol, holding in holdings.items():
            bar = current.get(symbol)
            mark = bar.open if bar is not None else last_prices.get(symbol, holding.last_price)
            nav_at_open += mark * Decimal(holding.quantity)
        maximum_allocation = nav_at_open * self.config.max_position_weight

        for index, (item, bar) in enumerate(eligible):
            remaining_names = len(eligible) - index
            budget = min(cash / Decimal(remaining_names), maximum_allocation)
            cash = self._buy(
                item.signal,
                session,
                timestamp,
                bar,
                budget,
                cash,
                holdings,
                trades,
                skipped,
                tradeability_verified=(item.signal.symbol, session) in constraints,
            )
        return cash, tuple(deferred)

    @staticmethod
    def _market_skip_detail(
        signal: Signal,
        timestamp: datetime,
        reason: str,
        constraints: Mapping[Tuple[str, date], TradingConstraint],
    ) -> str:
        if reason == "missing_execution_bar":
            return "no bar was supplied for this symbol and session; suspension is not inferred"
        constraint = constraints.get((signal.symbol, timestamp.date()))
        if constraint is not None and constraint.reason:
            return constraint.reason
        if reason == "explicit_suspended":
            return "explicit trading constraint marked the security suspended"
        if reason == "limit_up_locked":
            return "effective buy price reached or exceeded supplied limit_up_price"
        if reason == "limit_down_locked":
            return "effective sell price reached or fell below supplied limit_down_price"
        if reason == "zero_volume_execution_bar":
            return "the supplied execution bar had zero volume; suspension is not inferred"
        if reason == "missing_tradeability_constraint":
            return "strict execution policy requires an explicit point-in-time tradeability constraint"
        if reason == "volume_capacity_too_low":
            return "the participation cap permits less than one exchange lot on the execution bar"
        return "explicit trading constraint or zero-volume bar prevented execution"

    def _record_market_skip(
        self,
        item: _PendingSignal,
        timestamp: datetime,
        reason: str,
        constraints: Mapping[Tuple[str, date], TradingConstraint],
        skipped: List[SkippedExecution],
    ) -> Optional[_PendingSignal]:
        attempts_left = item.attempts_remaining - 1
        will_retry = attempts_left > 0
        skipped.append(
            SkippedExecution(
                signal=item.signal,
                attempted_at=timestamp,
                reason=reason,
                detail=self._market_skip_detail(
                    item.signal, timestamp, reason, constraints
                ),
                will_retry=will_retry,
            )
        )
        if not will_retry:
            return None
        return _PendingSignal(signal=item.signal, attempts_remaining=attempts_left)

    def _untradeable_reason(
        self,
        signal: Signal,
        session: date,
        bar: Optional[Bar],
        constraints: Mapping[Tuple[str, date], TradingConstraint],
    ) -> Optional[str]:
        constraint = constraints.get((signal.symbol, session))
        if constraint is not None:
            if constraint.suspended:
                return "explicit_suspended"
            if signal.action == SignalAction.BUY and not constraint.can_buy:
                return "explicit_buy_not_allowed"
            if signal.action == SignalAction.SELL and not constraint.can_sell:
                return "explicit_sell_not_allowed"
        if self.config.require_explicit_tradeability and constraint is None:
            return "missing_tradeability_constraint"
        if bar is None:
            return "missing_execution_bar"
        if bar.volume <= 0:
            return "zero_volume_execution_bar"
        if bar.open <= 0:
            return "invalid_execution_price"
        # A-share limit-lock state is supplied point-in-time by the caller;
        # never infer the percentage from a symbol prefix or previous close.
        # Compare the effective order price after configured slippage and tick
        # rounding so a fill cannot cross an exchange boundary merely because
        # execution assumptions were optimistic.
        if constraint is not None:
            tick = self._price_tick(signal.symbol)
            if signal.action == SignalAction.BUY and constraint.limit_up_price is not None:
                effective = self._price(
                    bar.open * (Decimal("1") + self.config.buy_slippage_rate),
                    tick,
                    side=SignalAction.BUY,
                )
                if effective >= constraint.limit_up_price:
                    return "limit_up_locked"
            if signal.action == SignalAction.SELL and constraint.limit_down_price is not None:
                effective = self._price(
                    bar.open * (Decimal("1") - self.config.sell_slippage_rate),
                    tick,
                    side=SignalAction.SELL,
                )
                if effective <= constraint.limit_down_price:
                    return "limit_down_locked"
        return None

    def _buy(
        self,
        signal: Signal,
        session: date,
        timestamp: datetime,
        bar: Bar,
        budget: Decimal,
        cash: Decimal,
        holdings: Dict[str, _Holding],
        trades: List[Trade],
        skipped: List[SkippedExecution],
        *,
        tradeability_verified: bool,
    ) -> Decimal:
        tick = self._price_tick(signal.symbol)
        price = self._price(
            bar.open * (Decimal("1") + self.config.buy_slippage_rate),
            tick,
            side=SignalAction.BUY,
        )
        lot_size = self._lot_size(signal.symbol)
        lot_notional = price * Decimal(lot_size)
        lots = int(budget // lot_notional) if lot_notional > 0 else 0
        quantity = lots * lot_size
        capacity_limited = False
        capacity_quantity = self._volume_capacity_quantity(bar)
        if capacity_quantity is not None:
            if capacity_quantity < lot_size:
                skipped.append(
                    SkippedExecution(
                        signal,
                        timestamp,
                        "volume_capacity_too_low",
                        detail="participation cap is below one exchange lot",
                        will_retry=False,
                    )
                )
                return cash
            if quantity > capacity_quantity:
                quantity = capacity_quantity
                capacity_limited = True
        while quantity > 0:
            gross = self._money(price * Decimal(quantity))
            commission = self._commission(gross, self.config.buy_commission_rate)
            if gross + commission <= budget and gross + commission <= cash:
                break
            quantity -= lot_size
        if quantity <= 0:
            skipped.append(
                SkippedExecution(signal, timestamp, "insufficient_cash_for_one_lot")
            )
            return cash
        gross = self._money(price * Decimal(quantity))
        commission = self._commission(gross, self.config.buy_commission_rate)
        debit = gross + commission
        cash = self._money(cash - debit)
        slippage = self._money((price - bar.open) * Decimal(quantity))
        holding = _Holding(symbol=signal.symbol, last_price=bar.open)
        holding.lots.append(_Lot(session, quantity, debit))
        holdings[signal.symbol] = holding
        trades.append(
            Trade(
                symbol=signal.symbol,
                action=SignalAction.BUY,
                signal_timestamp=signal.timestamp,
                execution_timestamp=timestamp,
                quantity=quantity,
                raw_price=bar.open,
                execution_price=price,
                gross_notional=gross,
                commission=commission,
                stamp_duty=Decimal("0.00"),
                slippage_cost=slippage,
                cash_after=cash,
                tradeability_verified=tradeability_verified,
            )
        )
        if capacity_limited:
            skipped.append(
                SkippedExecution(
                    signal,
                    timestamp,
                    "volume_participation_capped",
                    detail="buy quantity was reduced to the configured execution-bar participation cap",
                    will_retry=False,
                )
            )
        return cash

    def _sell(
        self,
        item: _PendingSignal,
        session: date,
        timestamp: datetime,
        current: Mapping[str, Bar],
        constraints: Mapping[Tuple[str, date], TradingConstraint],
        cash: Decimal,
        holdings: Dict[str, _Holding],
        trades: List[Trade],
        skipped: List[SkippedExecution],
    ) -> Tuple[Decimal, Optional[_PendingSignal]]:
        signal = item.signal
        bar = current.get(signal.symbol)
        reason = self._untradeable_reason(signal, session, bar, constraints)
        if reason is not None:
            return cash, self._record_market_skip(
                item, timestamp, reason, constraints, skipped
            )
        holding = holdings.get(signal.symbol)
        if holding is None or holding.quantity <= 0:
            skipped.append(
                SkippedExecution(signal, timestamp, "no_position", will_retry=False)
            )
            return cash, None
        quantity = holding.settled_quantity(session)
        if quantity <= 0:
            skipped.append(
                SkippedExecution(signal, timestamp, "t_plus_one", will_retry=False)
            )
            return cash, None
        assert bar is not None
        tick = self._price_tick(signal.symbol)
        price = self._price(
            bar.open * (Decimal("1") - self.config.sell_slippage_rate),
            tick,
            side=SignalAction.SELL,
        )
        if price <= 0:
            skipped.append(
                SkippedExecution(
                    signal, timestamp, "invalid_execution_price", will_retry=False
                )
            )
            return cash, None
        capacity_limited = False
        capacity_quantity = self._volume_capacity_quantity(bar)
        if capacity_quantity is not None:
            if capacity_quantity < self._lot_size(signal.symbol):
                skipped.append(
                    SkippedExecution(
                        signal,
                        timestamp,
                        "volume_capacity_too_low",
                        detail="participation cap is below one exchange lot",
                        will_retry=False,
                    )
                )
                return cash, None
            if quantity > capacity_quantity:
                quantity = capacity_quantity
                capacity_limited = True
        gross = self._money(price * Decimal(quantity))
        commission = self._commission(gross, self.config.sell_commission_rate)
        stamp_duty = self._money(gross * self.config.stamp_duty_rate)
        cash = self._money(cash + gross - commission - stamp_duty)
        removed_cost = holding.remove_settled(quantity, session)
        realized_pnl = self._money(gross - commission - stamp_duty - removed_cost)
        if holding.quantity == 0:
            del holdings[signal.symbol]
        slippage = self._money((bar.open - price) * Decimal(quantity))
        trades.append(
            Trade(
                symbol=signal.symbol,
                action=SignalAction.SELL,
                signal_timestamp=signal.timestamp,
                execution_timestamp=timestamp,
                quantity=quantity,
                raw_price=bar.open,
                execution_price=price,
                gross_notional=gross,
                commission=commission,
                stamp_duty=stamp_duty,
                slippage_cost=slippage,
                cash_after=cash,
                tradeability_verified=(signal.symbol, session) in constraints,
                realized_pnl=realized_pnl,
            )
        )
        if capacity_limited:
            skipped.append(
                SkippedExecution(
                    signal,
                    timestamp,
                    "volume_participation_capped",
                    detail="sell quantity was reduced to the configured execution-bar participation cap",
                    will_retry=False,
                )
            )
        return cash, None

    def _volume_capacity_quantity(self, bar: Bar) -> Optional[int]:
        """Return a lot-rounded maximum quantity for one execution bar.

        Daily bars expose aggregate volume only.  When an explicit
        participation cap is configured, the engine treats it as a bounded
        executable quantity and records any reduction as a skipped execution
        diagnostic.  ``None`` preserves the legacy exploratory model.
        """

        rate = self.config.max_volume_participation_rate
        if rate is None:
            return None
        lot_size = self._lot_size(bar.symbol)
        raw = int(Decimal(bar.volume) * rate)
        return (raw // lot_size) * lot_size

    def _lot_size(self, symbol: str) -> int:
        instrument = self._instruments.get(symbol)
        return instrument.lot_size if instrument is not None else self.config.lot_size

    def _price_tick(self, symbol: str) -> Decimal:
        instrument = self._instruments.get(symbol)
        return instrument.price_tick if instrument is not None else self.config.price_tick

    @staticmethod
    def _price(value: Decimal, tick: Decimal, *, side: SignalAction) -> Decimal:
        rounding = ROUND_CEILING if side == SignalAction.BUY else ROUND_FLOOR
        ticks = (value / tick).quantize(Decimal("1"), rounding=rounding)
        return ticks * tick

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _commission(self, gross: Decimal, rate: Decimal) -> Decimal:
        if rate == 0:
            return Decimal("0.00")
        return max(self._money(gross * rate), self.config.minimum_commission)

    def _equity_point(
        self,
        timestamp: datetime,
        cash: Decimal,
        holdings: Mapping[str, _Holding],
        previous: Sequence[EquityPoint],
    ) -> EquityPoint:
        market_value = self._money(
            sum(
                (holding.last_price * Decimal(holding.quantity) for holding in holdings.values()),
                Decimal("0"),
            )
        )
        nav = self._money(cash + market_value)
        prior_nav = previous[-1].nav if previous else self.config.initial_cash
        daily_return = float(nav / prior_nav - Decimal("1")) if prior_nav > 0 else 0.0
        prior_peak = max((item.nav for item in previous), default=self.config.initial_cash)
        peak = max(prior_peak, nav)
        drawdown = float(nav / peak - Decimal("1")) if peak > 0 else 0.0
        return EquityPoint(
            timestamp=timestamp,
            cash=cash,
            market_value=market_value,
            nav=nav,
            daily_return=daily_return,
            drawdown=drawdown,
        )

    def _positions(self, holdings: Mapping[str, _Holding], session: date) -> Dict[str, Position]:
        result = {}
        for symbol, holding in sorted(holdings.items()):
            quantity = holding.quantity
            total_cost = holding.total_cost
            market_value = self._money(holding.last_price * Decimal(quantity))
            result[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                settled_quantity=holding.settled_quantity(session),
                average_cost=(total_cost / Decimal(quantity)) if quantity else Decimal("0"),
                last_price=holding.last_price,
                market_value=market_value,
                unrealized_pnl=self._money(market_value - total_cost),
            )
        return result

    def _diagnostics(
        self,
        *,
        calendar_metadata: CalendarMetadata,
        bars: Mapping[str, Mapping[date, Bar]],
        sessions: Sequence[date],
        constraints: Mapping[Tuple[str, date], TradingConstraint],
        trades: Sequence[Trade],
        skipped: Sequence[SkippedExecution],
        universe_binding: Optional[Mapping[str, object]] = None,
        adjustment_binding: Optional[Mapping[str, object]] = None,
        calendar_fingerprint: Optional[str] = None,
        fundamental_binding: Optional[Mapping[str, object]] = None,
    ) -> BacktestDiagnostics:
        # A constraint is only meaningful for a symbol/session where an
        # execution bar actually exists.  Using ``len(bars) * len(sessions)``
        # here treats a symbol with a legitimately missing bar (for example a
        # sparse listing or a data gap) as if it had an executable session and
        # consequently understates coverage.  Keep the denominator aligned
        # with the engine's execution universe instead of penalising callers
        # for sessions that could never have produced a fill.
        session_set = set(sessions)
        possible_constraint_points = sum(
            1 for symbol_bars in bars.values() if symbol_bars
            for session in symbol_bars if session in session_set
        )
        constrained_points = sum(
            1
            for symbol, session in constraints
            # A constraint only covers an executable data point when this
            # symbol actually has an execution bar for the session.  A
            # caller may legitimately provide a calendar-wide constraint
            # snapshot that includes suspended/missing-bar dates; counting
            # those rows in the numerator would make it exceed the
            # denominator (and could even fail diagnostics validation with a
            # coverage greater than one).
            if symbol in bars
            and session in session_set
            and session in bars[symbol]
        )
        coverage = (
            constrained_points / possible_constraint_points
            if possible_constraint_points
            else 0.0
        )
        # Preserve the actual source identity of the bars consumed by this
        # replay.  Capacity/lineage reports must not infer it from the
        # currently configured provider: a provider switch after a result was
        # persisted would otherwise silently audit a different dataset (or
        # report every trade as unassessed).  Multiple sources remain visible
        # and are handled conservatively by downstream readers.
        bar_sources = tuple(sorted({
            str(bar.source).strip()
            for symbol_bars in bars.values()
            for bar in symbol_bars.values()
            if str(bar.source).strip()
        }))
        unverified = sum(not item.tradeability_verified for item in trades)
        warnings: List[str] = []
        if calendar_metadata.identifier in {
            "observed_sessions_fallback",
            "weekday_fallback",
        }:
            warnings.append("explicit_session_calendar_not_supplied")
        warnings.append("calendar_lineage_unverified")
        warnings.append("corporate_action_factor_lineage_unverified")
        if self.config.price_basis == "raw_unadjusted":
            warnings.append("raw_unadjusted_prices_may_include_corporate_action_gaps")
        if coverage < 1.0:
            warnings.append("tradeability_constraints_incomplete")
        if unverified:
            warnings.append("fills_without_explicit_tradeability_verification")
        warnings.append("daily_bar_availability_assumed_at_configured_evaluation_time")
        # The current pure engine accepts caller-supplied dates, prices and
        # constraint flags but cannot authenticate their provenance/version.
        # Therefore even apparently complete inputs remain research-grade.
        decision_grade = False
        return BacktestDiagnostics(
            calendar=calendar_metadata.identifier,
            calendar_source=calendar_metadata.source,
            calendar_version=calendar_metadata.version,
            # CalendarMetadata supplied through the structural Protocol is not
            # an attestation.  Until an authenticated official importer exists,
            # backtests must never surface caller-supplied verified=True.
            calendar_verified=False,
            price_basis=self.config.price_basis,
            bar_availability="assumed_at_{:02d}:{:02d}_asia_shanghai".format(
                self.config.daily_evaluation_time.hour,
                self.config.daily_evaluation_time.minute,
            ),
            execution_bar_availability="open_price_assumed_available_at_{:02d}:{:02d}_asia_shanghai".format(
                self.config.daily_execution_time.hour,
                self.config.daily_execution_time.minute,
            ),
            constraint_coverage=coverage,
            constrained_session_count=constrained_points,
            execution_attempt_count=len(trades) + len(
                [item for item in skipped if item.attempted_at is not None]
            ),
            unverified_fill_count=unverified,
            assumptions={
                "signal_execution": "next_session_open_with_bounded_validity",
                "strategy_price_basis": self.config.price_basis,
                # These are deliberately fixed even when strategy_price_basis
                # is qfq/hfq.  Corporate-action factors are a signal input,
                # not a substitute for raw market prices in accounting.
                "execution_price_basis": "raw_unadjusted",
                "valuation_price_basis": "raw_unadjusted",
                "missing_execution_bar": "unknown_availability_not_suspension",
                "unconstrained_positive_volume_bar": "fill_allowed_but_tradeability_unverified",
                "require_explicit_tradeability": self.config.require_explicit_tradeability,
                "max_volume_participation_rate": (
                    str(self.config.max_volume_participation_rate)
                    if self.config.max_volume_participation_rate is not None
                    else None
                ),
                "volume_execution_model": (
                    "bar_volume_participation_cap"
                    if self.config.max_volume_participation_rate is not None
                    else "unbounded_daily_bar"
                ),
                "portfolio_mapping": "sell_first_equal_cash_split_no_pyramiding",
                "bar_sources": bar_sources,
                "calendar_source": calendar_metadata.identifier,
                "calendar_fingerprint": calendar_fingerprint,
                "calendar_lineage": (
                    "unverified"
                ),
                "corporate_action_lineage": "unverified",
                "adjustment_factor_binding": (
                    dict(adjustment_binding)
                    if adjustment_binding is not None
                    else {"status": "not_bound", "factor_count": 0, "factor_versions": ()}
                ),
                "fundamental_factor_binding": (
                    dict(fundamental_binding)
                    if fundamental_binding is not None
                    else {"status": "not_bound", "report_count": 0}
                ),
                "universe_binding": (
                    dict(universe_binding)
                    if universe_binding is not None
                    else "not_bound"
                ),
            },
            warnings=tuple(warnings),
            decision_grade=decision_grade,
        )

    def _metrics(self, equity: Sequence[EquityPoint], trades: Sequence[Trade]) -> BacktestMetrics:
        starting = self.config.initial_cash
        ending = equity[-1].nav
        periods = max(len(equity) - 1, 0)
        ratio = float(ending / starting)
        total_return = ratio - 1.0
        if periods and ratio > 0:
            annualized_return = ratio ** (self.config.annualization_sessions / periods) - 1.0
        else:
            annualized_return = total_return
        daily_returns = [item.daily_return for item in equity[1:]]
        daily_risk_free = float(self.config.annual_risk_free_rate) / float(
            self.config.annualization_sessions
        )
        if len(daily_returns) >= 2:
            daily_volatility = statistics.stdev(daily_returns)
            annualized_volatility = daily_volatility * math.sqrt(
                self.config.annualization_sessions
            )
            sharpe = (
                (statistics.fmean(daily_returns) - daily_risk_free)
                / daily_volatility
                * math.sqrt(self.config.annualization_sessions)
                if daily_volatility > 0
                else 0.0
            )
        else:
            annualized_volatility = 0.0
            sharpe = 0.0
        maximum_drawdown = max((-item.drawdown for item in equity), default=0.0)
        excess_returns = [item - daily_risk_free for item in daily_returns]
        downside_squared = [min(item, 0.0) ** 2 for item in excess_returns]
        downside_deviation = (
            math.sqrt(statistics.fmean(downside_squared))
            * math.sqrt(self.config.annualization_sessions)
            if downside_squared
            else 0.0
        )
        sortino = (
            # Sortino's numerator is the arithmetic mean excess return,
            # annualized over the configured number of sessions.  Using the
            # CAGR here (as the old implementation did) mixes geometric and
            # arithmetic conventions and can materially over/understate the
            # ratio for volatile or short samples.
            (statistics.fmean(excess_returns) * self.config.annualization_sessions)
            / downside_deviation
            if downside_deviation > 0
            else 0.0
        )
        calmar = annualized_return / maximum_drawdown if maximum_drawdown > 0 else 0.0
        positive_session_ratio = (
            sum(item > 0 for item in daily_returns) / len(daily_returns)
            if daily_returns
            else 0.0
        )
        max_drawdown_duration = 0
        current_drawdown_duration = 0
        for point in equity:
            if point.drawdown < 0:
                current_drawdown_duration += 1
                max_drawdown_duration = max(
                    max_drawdown_duration, current_drawdown_duration
                )
            else:
                current_drawdown_duration = 0
        average_nav = sum((item.nav for item in equity), Decimal("0")) / Decimal(len(equity))
        traded_notional = sum((item.gross_notional for item in trades), Decimal("0"))
        turnover = float(traded_notional / average_nav) if average_nav > 0 else 0.0
        if any(item.action == SignalAction.SELL and item.realized_pnl is None for item in trades):
            raise ValueError("closing sell is missing realized_pnl")
        realized = tuple(
            item.realized_pnl
            for item in trades
            if item.action == SignalAction.SELL and item.realized_pnl is not None
        )
        winning = sum(value > 0 for value in realized)
        losing = sum(value < 0 for value in realized)
        break_even = sum(value == 0 for value in realized)
        closed = len(realized)
        gross_profit = sum((value for value in realized if value > 0), Decimal("0"))
        gross_loss = sum((-value for value in realized if value < 0), Decimal("0"))
        realized_pnl = sum(realized, Decimal("0"))
        win_rate = winning / closed if closed else None
        profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else None
        expectancy = realized_pnl / Decimal(closed) if closed else None
        return BacktestMetrics(
            starting_nav=starting,
            ending_nav=ending,
            total_return=total_return,
            annualized_return=annualized_return,
            annualized_volatility=annualized_volatility,
            sharpe_ratio=sharpe,
            maximum_drawdown=maximum_drawdown,
            turnover=turnover,
            total_commission=sum((item.commission for item in trades), Decimal("0")),
            total_stamp_duty=sum((item.stamp_duty for item in trades), Decimal("0")),
            total_slippage_cost=sum((item.slippage_cost for item in trades), Decimal("0")),
            session_count=len(equity),
            trade_count=len(trades),
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            positive_session_ratio=positive_session_ratio,
            maximum_drawdown_duration_sessions=max_drawdown_duration,
            closed_trade_count=closed,
            winning_trade_count=winning,
            losing_trade_count=losing,
            break_even_trade_count=break_even,
            win_rate=win_rate,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            realized_pnl=realized_pnl,
            profit_factor=profit_factor,
            expectancy_per_closed_trade=expectancy,
            trade_statistics_status="available" if closed else "no_closed_trades",
            profit_factor_status=(
                "available" if gross_loss > 0 else "no_losses" if closed else "no_closed_trades"
            ),
        )
