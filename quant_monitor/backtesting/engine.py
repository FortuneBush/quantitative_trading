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
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from quant_monitor.domain import (
    Bar,
    Instrument,
    SHANGHAI_TZ,
    Signal,
    SignalAction,
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
    ) -> BacktestResult:
        bars, observed_sessions = self._prepare_bars(bars_by_symbol)
        sessions, calendar_metadata = self._prepare_calendar(
            session_calendar, observed_sessions, trading_calendar
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
            evaluation_timestamp = self._session_timestamp(
                session, self.config.daily_evaluation_time
            )
            equity.append(self._equity_point(evaluation_timestamp, cash, holdings, equity))
            emitted = self._evaluate(session, evaluation_timestamp, bars, current)
            new_pending = self._validate_signals(
                emitted, evaluation_timestamp, seen_signal_keys
            )
            replacement_symbols = {item.signal.symbol for item in new_pending}
            pending = tuple(
                item
                for item in retry_pending
                if item.signal.symbol not in replacement_symbols
            ) + tuple(new_pending)

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

    def _prepare_bars(
        self, bars_by_symbol: Mapping[str, Sequence[Bar]]
    ) -> Tuple[Dict[str, Dict[date, Bar]], Tuple[date, ...]]:
        normalized: Dict[str, Dict[date, Bar]] = {}
        all_sessions: Set[date] = set()
        for raw_symbol, raw_bars in dict(bars_by_symbol or {}).items():
            symbol = normalize_symbol(raw_symbol)
            if symbol in normalized:
                raise BacktestValidationError("duplicate normalized symbol: {}".format(symbol))
            by_session: Dict[date, Bar] = {}
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
                by_session[session] = bar
                all_sessions.add(session)
            normalized[symbol] = by_session
        if not all_sessions:
            raise BacktestValidationError("at least one daily bar is required")
        return normalized, tuple(sorted(all_sessions))

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
            missing = [item for item in observed_sessions if item not in set(sessions)]
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
        missing = [item for item in observed_sessions if item not in session_set]
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
        if reason == "zero_volume_execution_bar":
            return "the supplied execution bar had zero volume; suspension is not inferred"
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
        if bar is None:
            return "missing_execution_bar"
        if bar.volume <= 0:
            return "zero_volume_execution_bar"
        if bar.open <= 0:
            return "invalid_execution_price"
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
        gross = self._money(price * Decimal(quantity))
        commission = self._commission(gross, self.config.sell_commission_rate)
        stamp_duty = self._money(gross * self.config.stamp_duty_rate)
        cash = self._money(cash + gross - commission - stamp_duty)
        holding.remove_settled(quantity, session)
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
            )
        )
        return cash, None

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
    ) -> BacktestDiagnostics:
        possible_constraint_points = len(bars) * len(sessions)
        constrained_points = sum(
            1
            for symbol, session in constraints
            if symbol in bars and session in set(sessions)
        )
        coverage = (
            constrained_points / possible_constraint_points
            if possible_constraint_points
            else 0.0
        )
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
                "missing_execution_bar": "unknown_availability_not_suspension",
                "unconstrained_positive_volume_bar": "fill_allowed_but_tradeability_unverified",
                "portfolio_mapping": "sell_first_equal_cash_split_no_pyramiding",
                "calendar_source": calendar_metadata.identifier,
                "calendar_lineage": (
                    "unverified"
                ),
                "corporate_action_lineage": "unverified",
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
        if len(daily_returns) >= 2:
            daily_volatility = statistics.stdev(daily_returns)
            annualized_volatility = daily_volatility * math.sqrt(
                self.config.annualization_sessions
            )
            daily_risk_free = float(self.config.annual_risk_free_rate) / float(
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
        average_nav = sum((item.nav for item in equity), Decimal("0")) / Decimal(len(equity))
        traded_notional = sum((item.gross_notional for item in trades), Decimal("0"))
        turnover = float(traded_notional / average_nav) if average_nav > 0 else 0.0
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
        )
