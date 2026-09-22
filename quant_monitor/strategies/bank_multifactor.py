"""Cross-sectional daily-price research model for listed bank stocks.

The model deliberately uses only normalized, completed daily OHLCV bars.  It
does not access a provider, persistence, a clock or an order interface.  The
input bars are raw/unadjusted under the current AShareHub contract; a simple
gap detector therefore suspends affected observations instead of treating a
possible ex-dividend move as genuine momentum.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from quant_monitor.domain import Bar, Signal, SignalAction, ensure_market_time, normalize_symbol

from .base import CohortStrategy, CohortStrategyContext, StrategyContext


@dataclass(frozen=True)
class _Observation:
    symbol: str
    latest: Bar
    sma_20: float
    sma_60: float
    sma_120: float
    price_to_sma_120: float
    sma_20_to_60: float
    momentum_60_5: float
    momentum_120_20: float
    annualized_volatility_60: float
    max_drawdown_60: float
    average_turnover_20: float
    review_due: bool
    risk_exit: bool
    trend_exit: bool
    suspected_corporate_action: bool


class BankDailyMultifactorStrategy(CohortStrategy):
    """Rank bank shares using trend, momentum, low risk and liquidity gates.

    Signals are research classifications only. ``BUY`` means that a symbol
    entered the configured candidate bucket, while ``SELL`` means exit/risk
    avoidance; neither action is an order or a recommendation to transact.
    """

    MODEL_VERSION = "bank_daily_multifactor_v1"
    PRICE_BASIS = "raw_unadjusted"
    # The longest factor needs 121 rows (120-session momentum plus its base).
    # Corporate-action suspicion is evaluated over exactly this causal window
    # rather than over arbitrary older history supplied by a cache.
    MAX_FACTOR_LOOKBACK = 121

    def __init__(
        self,
        *,
        min_history: int = 130,
        min_universe: int = 15,
        entry_percentile: float = 0.80,
        exit_percentile: float = 0.50,
        minimum_breadth: float = 0.40,
        maximum_annualized_volatility: float = 0.45,
        maximum_drawdown: float = 0.18,
        liquidity_floor_percentile: float = 0.10,
        corporate_action_gap: float = 0.06,
        trend_exit_days: int = 2,
        valid_sessions: int = 5,
        fundamental_weight: float = 0.20,
        strategy_name: str = MODEL_VERSION,
    ) -> None:
        self.min_history = int(min_history)
        self.min_universe = int(min_universe)
        self.entry_percentile = float(entry_percentile)
        self.exit_percentile = float(exit_percentile)
        self.minimum_breadth = float(minimum_breadth)
        self.maximum_annualized_volatility = float(maximum_annualized_volatility)
        self.maximum_drawdown = float(maximum_drawdown)
        self.liquidity_floor_percentile = float(liquidity_floor_percentile)
        self.corporate_action_gap = float(corporate_action_gap)
        self.trend_exit_days = int(trend_exit_days)
        self.valid_sessions = int(valid_sessions)
        self.fundamental_weight = float(fundamental_weight)
        self._name = str(strategy_name).strip()
        if self.min_history < 121:
            raise ValueError("min_history must be at least 121")
        if self.min_universe < 2:
            raise ValueError("min_universe must be at least 2")
        if not 0.0 <= self.exit_percentile < self.entry_percentile <= 1.0:
            raise ValueError("percentiles must satisfy 0 <= exit < entry <= 1")
        for name, value in (
            ("minimum_breadth", self.minimum_breadth),
            ("liquidity_floor_percentile", self.liquidity_floor_percentile),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("{} must be between 0 and 1".format(name))
        if self.maximum_annualized_volatility <= 0:
            raise ValueError("maximum_annualized_volatility must be positive")
        if not 0.0 < self.maximum_drawdown < 1.0:
            raise ValueError("maximum_drawdown must be between 0 and 1")
        if not 0.0 < self.corporate_action_gap < 1.0:
            raise ValueError("corporate_action_gap must be between 0 and 1")
        if self.trend_exit_days < 1:
            raise ValueError("trend_exit_days must be positive")
        if self.valid_sessions < 1:
            raise ValueError("valid_sessions must be positive")
        if not math.isfinite(self.fundamental_weight) or not 0.0 <= self.fundamental_weight < 1.0:
            raise ValueError("fundamental_weight must be between 0 and 1")
        if not self._name:
            raise ValueError("strategy_name cannot be empty")

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def _sma(values: Sequence[float], window: int) -> float:
        return statistics.fmean(values[-window:])

    @staticmethod
    def _annualized_volatility(values: Sequence[float], window: int = 60) -> float:
        recent = values[-(window + 1) :]
        returns = [current / previous - 1.0 for previous, current in zip(recent, recent[1:])]
        return statistics.stdev(returns) * math.sqrt(252.0)

    @staticmethod
    def _max_drawdown(values: Sequence[float], window: int = 60) -> float:
        peak = values[-window]
        worst = 0.0
        for value in values[-window:]:
            peak = max(peak, value)
            worst = min(worst, value / peak - 1.0)
        return worst

    def _suspected_corporate_action(self, bars: Sequence[Bar]) -> bool:
        """Detect discontinuities anywhere in the factor lookback window.

        The strategy uses up to 120 sessions of price history for its factors.
        Restricting the check to only the last few bars allows an older
        ex-dividend/split gap to remain in the momentum and drawdown inputs,
        which can create a false cross-sectional ranking.  We intentionally
        keep this as a conservative *suspicion* gate (rather than attempting
        to infer an adjustment factor); a provider with corporate-action
        lineage can later replace the raw bars with adjusted observations.
        """
        for previous, current in zip(bars, bars[1:]):
            previous_close = float(previous.close)
            if previous_close <= 0:
                return True
            gap = abs(float(current.open) / previous_close - 1.0)
            outside_range = previous.close < current.low or previous.close > current.high
            if gap > self.corporate_action_gap and outside_range:
                return True
        return False

    def _observation(self, context: StrategyContext) -> Optional[_Observation]:
        bars = [bar for bar in context.bars if bar.interval == "1d"]
        if len(bars) != len(context.bars) or len(bars) < self.min_history:
            return None
        if bars[-1].timestamp > context.timestamp:
            return None
        if any(left.timestamp.date() >= right.timestamp.date() for left, right in zip(bars, bars[1:])):
            return None
        latest = bars[-1]
        closes = [float(bar.close) for bar in bars]
        if any(value <= 0 or not math.isfinite(value) for value in closes):
            return None
        if any(bar.volume <= 0 for bar in bars[-20:]):
            return None
        sma_20 = self._sma(closes, 20)
        sma_60 = self._sma(closes, 60)
        sma_120 = self._sma(closes, 120)
        volatility = self._annualized_volatility(closes, 60)
        drawdown = self._max_drawdown(closes, 60)
        turnovers = [float(bar.turnover) for bar in bars[-20:]]
        average_turnover = statistics.fmean(turnovers)
        if average_turnover <= 0 or not math.isfinite(average_turnover):
            return None
        latest_iso = latest.timestamp.isocalendar()
        previous_iso = bars[-2].timestamp.isocalendar()
        review_due = (latest_iso[0], latest_iso[1]) != (previous_iso[0], previous_iso[1])
        trend_exit = all(
            float(bar.close) < statistics.fmean(closes[index - 59 : index + 1])
            for index, bar in list(enumerate(bars))[-self.trend_exit_days :]
            if index >= 59
        )
        return _Observation(
            symbol=context.symbol,
            latest=latest,
            sma_20=sma_20,
            sma_60=sma_60,
            sma_120=sma_120,
            price_to_sma_120=closes[-1] / sma_120 - 1.0,
            sma_20_to_60=sma_20 / sma_60 - 1.0,
            momentum_60_5=closes[-6] / closes[-61] - 1.0,
            momentum_120_20=closes[-21] / closes[-121] - 1.0,
            annualized_volatility_60=volatility,
            max_drawdown_60=drawdown,
            average_turnover_20=average_turnover,
            review_due=review_due,
            risk_exit=(
                volatility > self.maximum_annualized_volatility
                or drawdown <= -self.maximum_drawdown
            ),
            trend_exit=trend_exit,
            suspected_corporate_action=self._suspected_corporate_action(
                bars[-self.MAX_FACTOR_LOOKBACK :]
            ),
        )

    @staticmethod
    def _percentile_ranks(
        observations: Sequence[_Observation], getter, *, higher_is_better: bool = True
    ) -> Dict[str, float]:
        ordered = sorted(
            observations,
            key=lambda item: ((getter(item) if higher_is_better else -getter(item)), item.symbol),
        )
        count = len(ordered)
        if count == 1:
            return {ordered[0].symbol: 1.0}
        ranks: Dict[str, float] = {}
        start = 0
        while start < count:
            raw_value = getter(ordered[start])
            end = start + 1
            while end < count and getter(ordered[end]) == raw_value:
                end += 1
            average_index = (start + end - 1) / 2.0
            percentile = average_index / (count - 1)
            for item in ordered[start:end]:
                ranks[item.symbol] = percentile
            start = end
        return ranks

    @staticmethod
    def _rank_from_top(scores: Mapping[str, float], symbol: str) -> int:
        """Return a competition rank without using symbol order as a tiebreaker.

        A deterministic symbol sort is useful when rendering a table, but it
        must not leak into the reported cross-sectional rank: two securities
        with the same composite score are equally ranked.  The previous
        implementation returned different ranks for ties, which made the UI
        imply a spurious preference even though entry eligibility was based on
        the score itself.
        """
        if symbol not in scores:
            raise KeyError(symbol)
        target = float(scores[symbol])
        return 1 + sum(1 for value in scores.values() if float(value) > target)

    def _fundamental_overlay(
        self,
        context: CohortStrategyContext,
        observations: Sequence[_Observation],
    ) -> Mapping[str, Any]:
        """Validate an optional point-in-time factor report without I/O."""

        weight = self.fundamental_weight
        base: Dict[str, Any] = {
            "enabled": False,
            "status": "disabled" if weight == 0.0 else "unavailable",
            "weight": weight,
            "reason": (
                "fundamental_overlay_disabled"
                if weight == 0.0
                else "fundamental_data_unavailable_price_only_fallback"
            ),
            "decision_at": None,
            "scores": {},
        }
        if weight == 0.0:
            return base
        report = context.metadata.get("fundamental_factor_report")
        if not isinstance(report, Mapping):
            return base
        if report.get("selection") != "bank_fundamental_factors_as_of_v1":
            base["reason"] = "fundamental_report_selection_invalid"
            return base
        try:
            decision_at = ensure_market_time(
                datetime.fromisoformat(str(report["decision_at"]))
            )
        except (KeyError, TypeError, ValueError):
            base["reason"] = "fundamental_report_decision_time_invalid"
            return base
        base["decision_at"] = decision_at.isoformat()
        if decision_at > context.timestamp:
            base["reason"] = "fundamental_report_future_of_decision"
            return base
        quality = report.get("quality")
        if not isinstance(quality, Mapping) or quality.get("status") != "ok":
            base["reason"] = "fundamental_report_not_complete"
            return base
        if report.get("warnings") not in (None, [], ()):
            base["reason"] = "fundamental_report_has_warnings"
            return base
        items = report.get("items")
        if not isinstance(items, (list, tuple)) or len(items) < 2:
            base["reason"] = "fundamental_report_cross_section_too_small"
            return base
        if quality.get("complete_count") != len(items):
            base["reason"] = "fundamental_report_coverage_mismatch"
            return base
        expected_symbols = {item.symbol for item in observations}
        scores: Dict[str, float] = {}
        for item in items:
            if not isinstance(item, Mapping):
                base["reason"] = "fundamental_report_item_invalid"
                return base
            try:
                symbol = normalize_symbol(item["symbol"])
                score = item["score"]
            except (KeyError, TypeError, ValueError):
                base["reason"] = "fundamental_report_item_invalid"
                return base
            if item.get("score_status") != "complete" or isinstance(score, bool):
                base["reason"] = "fundamental_report_score_unavailable"
                return base
            if any(
                item.get(key)
                for key in (
                    "missing_metrics",
                    "invalid_unit_metrics",
                    "invalid_scope_metrics",
                    "incomparable_period_metrics",
                )
            ) or item.get("source_conflict") or item.get("scope_conflict"):
                base["reason"] = "fundamental_report_item_incomplete"
                return base
            try:
                score = float(score)
            except (TypeError, ValueError):
                base["reason"] = "fundamental_report_score_invalid"
                return base
            if not math.isfinite(score) or not 0.0 <= score <= 1.0 or symbol in scores:
                base["reason"] = "fundamental_report_score_invalid"
                return base
            scores[symbol] = score
        if set(scores) != expected_symbols:
            base["reason"] = "fundamental_report_universe_mismatch"
            return base
        base.update({
            "enabled": True,
            "status": "applied",
            "reason": "fundamental_overlay_applied",
            "scores": scores,
        })
        return base

    def evaluate(self, context: CohortStrategyContext) -> Sequence[Signal]:
        snapshot = self.analyze(context)
        if not snapshot["ready"]:
            return ()
        signals: List[Signal] = []
        for item in snapshot["items"]:
            if item.get("signal") is not None:
                signals.append(item["signal"])
        return tuple(signals)

    def analyze(self, context: CohortStrategyContext) -> Mapping[str, object]:
        """Return cached-bar research states for every valid cohort member.

        This shares the exact same factor computation and gates as signal
        generation.  API callers can therefore show neutral/insufficient
        states without fabricating a Signal or performing any I/O.
        """

        # Live provider observations currently carry degraded/unverified
        # lineage until a vendor supplies accepted availability and revision
        # evidence.  The monitoring orchestrator can explicitly mark such a
        # cohort as not decision-ready; fail closed before ranking so a
        # mathematically valid cross-sectional score cannot become a BUY/SELL
        # signal. Direct pure-strategy/backtest callers that do not provide
        # this optional marker retain deterministic behavior.
        if context.metadata.get("strategy_decision_ready") is False:
            excluded = {
                symbol: "data_quality_not_decision_ready"
                for symbol in context.members
            }
            return {
                "ready": False,
                "reason": "data_quality_not_decision_ready",
                "valid_universe": 0,
                "required_universe": self.min_universe,
                "excluded": excluded,
                "items": (),
            }

        observations = []
        excluded = {}
        for symbol, member in context.members.items():
            observation = self._observation(member)
            if observation is None:
                excluded[symbol] = "insufficient_or_invalid_history"
            elif observation.latest.timestamp > context.timestamp:
                excluded[symbol] = "future_bar"
            elif observation.suspected_corporate_action:
                excluded[symbol] = "suspected_corporate_action"
            else:
                observations.append(observation)
        if len(observations) < self.min_universe:
            return {
                "ready": False,
                "reason": "minimum_universe_not_met",
                "valid_universe": len(observations),
                "required_universe": self.min_universe,
                "excluded": excluded,
                "items": (),
            }
        as_of_dates = {item.latest.timestamp.date() for item in observations}
        if len(as_of_dates) != 1:
            return {
                "ready": False,
                "reason": "common_as_of_not_met",
                "valid_universe": len(observations),
                "required_universe": self.min_universe,
                "excluded": excluded,
                "items": (),
            }
        expected_as_of = context.metadata.get("expected_bar_date")
        if expected_as_of is not None:
            expected_text = str(expected_as_of)
            actual_text = next(iter(as_of_dates)).isoformat()
            if actual_text != expected_text:
                return {
                    "ready": False,
                    "reason": "latest_expected_session_not_met",
                    "valid_universe": len(observations),
                    "required_universe": self.min_universe,
                    "expected_bar_date": expected_text,
                    "actual_bar_date": actual_text,
                    "excluded": excluded,
                    "items": (),
                }

        factor_specs = (
            ("price_to_sma_120", lambda item: item.price_to_sma_120, True, 0.15),
            ("sma_20_to_60", lambda item: item.sma_20_to_60, True, 0.15),
            ("momentum_60_5", lambda item: item.momentum_60_5, True, 0.20),
            ("momentum_120_20", lambda item: item.momentum_120_20, True, 0.15),
            (
                "low_volatility_60",
                lambda item: item.annualized_volatility_60,
                False,
                0.20,
            ),
            ("shallow_drawdown_60", lambda item: item.max_drawdown_60, True, 0.15),
        )
        factor_weights = {name: weight for name, _getter, _higher, weight in factor_specs}
        ranks: Dict[str, Dict[str, float]] = {item.symbol: {} for item in observations}
        scores = {item.symbol: 0.0 for item in observations}
        for name, getter, higher_is_better, weight in factor_specs:
            factor_ranks = self._percentile_ranks(
                observations, getter, higher_is_better=higher_is_better
            )
            for symbol, value in factor_ranks.items():
                ranks[symbol][name] = value
                scores[symbol] += value * weight
        liquidity_ranks = self._percentile_ranks(
            observations, lambda item: item.average_turnover_20
        )
        fundamental_overlay = self._fundamental_overlay(context, observations)
        price_scores = dict(scores)
        if fundamental_overlay["enabled"]:
            overlay_scores = fundamental_overlay["scores"]
            for symbol in scores:
                scores[symbol] = (
                    price_scores[symbol] * (1.0 - self.fundamental_weight)
                    + overlay_scores[symbol] * self.fundamental_weight
                )
        breadth = statistics.fmean(
            float(item.latest.close) > item.sma_120 for item in observations
        )

        results = []
        for item in sorted(observations, key=lambda value: value.symbol):
            score = scores[item.symbol]
            fundamental_score = (
                float(fundamental_overlay["scores"][item.symbol])
                if fundamental_overlay["enabled"]
                else None
            )
            fundamental_contribution = (
                fundamental_score * self.fundamental_weight
                if fundamental_score is not None
                else 0.0
            )
            factor_contributions = {
                name: ranks[item.symbol][name] * weight
                for name, weight in factor_weights.items()
            }
            gates = {
                "review_due": item.review_due,
                "breadth_ok": breadth >= self.minimum_breadth,
                "price_above_sma_120": float(item.latest.close) > item.sma_120,
                "sma_20_above_sma_60": item.sma_20 > item.sma_60,
                "momentum_60_5_positive": item.momentum_60_5 > 0,
                "volatility_ok": (
                    item.annualized_volatility_60 <= self.maximum_annualized_volatility
                ),
                "drawdown_ok": item.max_drawdown_60 > -self.maximum_drawdown,
                "liquidity_ok": (
                    liquidity_ranks[item.symbol] >= self.liquidity_floor_percentile
                ),
            }
            # Keep the decision path explicit for UI/API consumers.  A plain
            # action (BUY/HOLD/SELL) is not enough to explain why a candidate
            # was rejected or why a risk exit won; expose stable gate names so
            # downstream clients do not have to reverse-engineer thresholds.
            gate_failures = tuple(
                name for name, passed in gates.items() if not bool(passed)
            )
            # ``gate_failures`` describes the boolean health gates above, but
            # a symbol can still be ineligible when every gate passes: the
            # cross-sectional score may be below the entry threshold or the
            # weekly review may not be due yet.  Keep these causes separate
            # and explicit so a HOLD is not mistaken for an unexplained
            # neutral recommendation.
            entry_failure_reasons = []
            if not item.review_due:
                entry_failure_reasons.append("review_not_due")
            if score < self.entry_percentile:
                entry_failure_reasons.append("score_below_entry_percentile")
            entry_failure_reasons.extend(
                name for name, passed in gates.items()
                if name != "review_due" and not bool(passed)
            )
            entry_failure_reasons = tuple(dict.fromkeys(entry_failure_reasons))
            entry = (
                item.review_due
                and score >= self.entry_percentile
                and all(value for name, value in gates.items() if name != "review_due")
            )
            exit_signal = item.risk_exit or item.trend_exit or (
                item.review_due and score < self.exit_percentile
            )
            exit_reasons = []
            if item.risk_exit:
                exit_reasons.append("risk_limit")
            if item.trend_exit:
                exit_reasons.append("trend_below_sma_60")
            if item.review_due and score < self.exit_percentile:
                exit_reasons.append("score_below_exit_percentile")
            # Daily risk exits override a coincident scheduled entry review.
            # This makes risk gating conservative without carrying positions
            # or hidden mutable state inside the strategy.
            action = (
                SignalAction.SELL
                if exit_signal
                else (SignalAction.BUY if entry else SignalAction.HOLD)
            )
            reason = (
                "进入银行股多因子研究候选"
                if action == SignalAction.BUY
                else (
                    "退出银行股研究候选或进入风险回避观察"
                    if action == SignalAction.SELL
                    else "银行股多因子中性观察"
                )
            )
            signed_strength = (
                score
                if action == SignalAction.BUY
                else (-max(1.0 - score, 0.01) if action == SignalAction.SELL else 0.0)
            )
            signal = None
            if action != SignalAction.HOLD:
                signal = Signal(
                    strategy=self.name,
                    symbol=item.symbol,
                    timestamp=item.latest.timestamp,
                    action=action,
                    strength=max(-1.0, min(1.0, signed_strength)),
                    price=item.latest.close,
                    reason=reason,
                    metadata={
                        "model_version": self.MODEL_VERSION,
                        "model_variant": (
                            "price_plus_fundamental_v1"
                            if fundamental_overlay["enabled"]
                            else "price_only_v1"
                        ),
                        "bar_as_of": item.latest.timestamp.isoformat(),
                        "price_basis": self.PRICE_BASIS,
                        "cohort": context.cohort,
                        "universe_size": len(observations),
                        "rank": self._rank_from_top(scores, item.symbol),
                        "percentile": score,
                        "price_score": price_scores[item.symbol],
                        "fundamental_overlay": {
                            "enabled": fundamental_overlay["enabled"],
                            "status": fundamental_overlay["status"],
                            "weight": self.fundamental_weight,
                            "score": fundamental_score,
                            "contribution": fundamental_contribution,
                            "reason": fundamental_overlay["reason"],
                            "decision_at": fundamental_overlay["decision_at"],
                        },
                        "score_thresholds": {
                            "entry": self.entry_percentile,
                            "exit": self.exit_percentile,
                        },
                        "factor_weights": factor_weights,
                        "factor_contributions": factor_contributions,
                        "factors": {
                            "price_to_sma_120": item.price_to_sma_120,
                            "sma_20_to_60": item.sma_20_to_60,
                            "momentum_60_5": item.momentum_60_5,
                            "momentum_120_20": item.momentum_120_20,
                            "annualized_volatility_60": item.annualized_volatility_60,
                            "max_drawdown_60": item.max_drawdown_60,
                            "average_turnover_20": item.average_turnover_20,
                        },
                        "factor_percentiles": ranks[item.symbol],
                        "gates": gates,
                        "gate_failures": gate_failures,
                        "entry_failure_reasons": entry_failure_reasons,
                        "entry_eligible": entry,
                        "exit_reasons": tuple(exit_reasons),
                        "breadth": breadth,
                        "risk_exit": item.risk_exit,
                        "trend_exit": item.trend_exit,
                        "suspected_corporate_action": False,
                        "valid_sessions": self.valid_sessions,
                        "signal_only": True,
                    },
                )
            results.append(
                {
                    "symbol": item.symbol,
                    "model_variant": (
                        "price_plus_fundamental_v1"
                        if fundamental_overlay["enabled"]
                        else "price_only_v1"
                    ),
                    "bar_as_of": item.latest.timestamp.isoformat(),
                    "price": item.latest.close,
                    "score": score,
                    "price_score": price_scores[item.symbol],
                    "rank": self._rank_from_top(scores, item.symbol),
                    "universe_size": len(observations),
                    "action": action.value,
                    "score_thresholds": {
                        "entry": self.entry_percentile,
                        "exit": self.exit_percentile,
                    },
                    "factor_weights": factor_weights,
                    "factor_contributions": factor_contributions,
                    "fundamental_overlay": {
                        "enabled": fundamental_overlay["enabled"],
                        "status": fundamental_overlay["status"],
                        "weight": self.fundamental_weight,
                        "score": fundamental_score,
                        "contribution": fundamental_contribution,
                        "reason": fundamental_overlay["reason"],
                        "decision_at": fundamental_overlay["decision_at"],
                    },
                    "label": {
                        SignalAction.BUY: "偏强研究",
                        SignalAction.SELL: "偏弱研究",
                        SignalAction.HOLD: "中性观察",
                    }[action],
                    "reason": reason,
                    "factors": {
                        "price_to_sma_120": item.price_to_sma_120,
                        "sma_20_to_60": item.sma_20_to_60,
                        "momentum_60_5": item.momentum_60_5,
                        "momentum_120_20": item.momentum_120_20,
                        "annualized_volatility_60": item.annualized_volatility_60,
                        "max_drawdown_60": item.max_drawdown_60,
                        "average_turnover_20": item.average_turnover_20,
                    },
                    "factor_percentiles": ranks[item.symbol],
                    "gates": gates,
                    "gate_failures": gate_failures,
                    "entry_failure_reasons": entry_failure_reasons,
                    "entry_eligible": entry,
                    "exit_reasons": tuple(exit_reasons),
                    "breadth": breadth,
                    "risk_exit": item.risk_exit,
                    "trend_exit": item.trend_exit,
                    "price_basis": self.PRICE_BASIS,
                    "signal": signal,
                }
            )
        return {
            "ready": True,
            "reason": None,
            "bar_as_of": next(iter(as_of_dates)).isoformat(),
            "valid_universe": len(observations),
            "required_universe": self.min_universe,
            "breadth": breadth,
            "excluded": excluded,
            "fundamental_overlay": {
                "enabled": fundamental_overlay["enabled"],
                "status": fundamental_overlay["status"],
                "weight": self.fundamental_weight,
                "reason": fundamental_overlay["reason"],
                "decision_at": fundamental_overlay["decision_at"],
            },
            "items": tuple(results),
        }
