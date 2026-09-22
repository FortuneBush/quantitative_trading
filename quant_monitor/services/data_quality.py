"""Deterministic, offline quality audit for normalized daily bars.

The service deliberately accepts domain ``Bar`` objects rather than vendor
payloads.  OHLC, non-negative volume, symbol normalization and timezone rules
have therefore already been enforced by the domain model.  This module checks
the properties that only become visible across rows and symbols: ordering,
duplicate sessions, missing history, freshness and cross-sectional alignment.

The built-in calendar is the project's documented weekday fallback.  It must
not be confused with an official exchange holiday calendar; the limitation is
made explicit in every audit result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from quant_monitor.domain import Bar, ensure_market_time, normalize_symbol
from quant_monitor.market import expected_latest_daily_date
from quant_monitor.trading_calendar import (
    CalendarRangeError,
    DEFAULT_TRADING_CALENDAR,
    TradingCalendar,
)


@dataclass(frozen=True)
class SymbolDailyQuality:
    """Stable, JSON-safe quality result for one normalized symbol."""

    symbol: str
    status: str
    rows_received: int
    unique_dates: int
    first_date: Optional[str]
    latest_date: Optional[str]
    latest_usable_date: Optional[str]
    expected_latest_date: str
    latest_expected_present: bool
    required_history_bars: int
    available_history_bars: int
    history_shortfall: int
    history_coverage_ratio: float
    stale_session_count: int
    missing_date_count: int
    missing_dates: Tuple[str, ...]
    duplicate_row_count: int
    duplicate_dates: Tuple[str, ...]
    out_of_order_count: int
    future_date_count: int
    future_dates: Tuple[str, ...]
    off_calendar_date_count: int
    off_calendar_dates: Tuple[str, ...]
    sources: Tuple[str, ...]
    source_consistent: bool
    source_complete: bool
    missing_source_row_count: int
    # Missing rows are expressed in trading-session order, not calendar-day
    # distance.  This keeps a holiday from looking like a multi-day data gap.
    max_missing_streak: int = 0
    missing_session_ranges: Tuple[str, ...] = ()
    calendar_out_of_coverage_date_count: int = 0
    calendar_out_of_coverage_dates: Tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-native mapping with a version-stable field layout."""

        return {
            "symbol": self.symbol,
            "status": self.status,
            "healthy": self.healthy,
            "rows_received": self.rows_received,
            "unique_dates": self.unique_dates,
            "first_date": self.first_date,
            "latest_date": self.latest_date,
            "latest_usable_date": self.latest_usable_date,
            "expected_latest_date": self.expected_latest_date,
            "latest_expected_present": self.latest_expected_present,
            "required_history_bars": self.required_history_bars,
            "available_history_bars": self.available_history_bars,
            "history_shortfall": self.history_shortfall,
            "history_coverage_ratio": self.history_coverage_ratio,
            "stale_session_count": self.stale_session_count,
            "missing_date_count": self.missing_date_count,
            "missing_dates": list(self.missing_dates),
            "duplicate_row_count": self.duplicate_row_count,
            "duplicate_dates": list(self.duplicate_dates),
            "out_of_order_count": self.out_of_order_count,
            "future_date_count": self.future_date_count,
            "future_dates": list(self.future_dates),
            "off_calendar_date_count": self.off_calendar_date_count,
            "off_calendar_dates": list(self.off_calendar_dates),
            "sources": list(self.sources),
            "source_consistent": self.source_consistent,
            "source_complete": self.source_complete,
            "missing_source_row_count": self.missing_source_row_count,
            "max_missing_streak": self.max_missing_streak,
            "missing_session_ranges": list(self.missing_session_ranges),
            "calendar_out_of_coverage_date_count": self.calendar_out_of_coverage_date_count,
            "calendar_out_of_coverage_dates": list(self.calendar_out_of_coverage_dates),
        }


@dataclass(frozen=True)
class DailyDataQualitySummary:
    """Immutable multi-symbol audit summary with JSON-safe serialization."""

    schema_version: int
    status: str
    as_of: str
    interval: str
    calendar: str
    calendar_notice: str
    calendar_source: str
    calendar_version: str
    calendar_verified: bool
    expected_latest_date: str
    required_history_bars: int
    expected_symbol_count: int
    observed_symbol_count: int
    healthy_symbol_count: int
    missing_symbols: Tuple[str, ...]
    unexpected_symbols: Tuple[str, ...]
    expected_date_symbol_count: int
    expected_date_coverage_ratio: float
    common_date_count: int
    latest_common_date: Optional[str]
    common_dates: Tuple[str, ...]
    sources: Tuple[str, ...]
    mixed_source_symbols: Tuple[str, ...]
    missing_source_symbols: Tuple[str, ...]
    source_consistent: bool
    symbols: Tuple[SymbolDailyQuality, ...]
    # The calendar identity and finite coverage are part of the quality
    # result, so consumers can tell whether a gap is inside a known calendar
    # window or outside the offline fixture's evidence.
    calendar_coverage_start: Optional[str] = None
    calendar_coverage_end: Optional[str] = None
    # A finite VersionedSessionCalendar supplies a deterministic snapshot
    # digest; fallback and arbitrary injected calendars remain explicitly
    # un-fingerprinted rather than inventing provenance.
    calendar_fingerprint: Optional[str] = None
    max_missing_streak: int = 0

    @property
    def healthy(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        """Return only JSON-native scalars, lists and mappings."""

        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "healthy": self.healthy,
            "as_of": self.as_of,
            "interval": self.interval,
            "calendar": self.calendar,
            "calendar_notice": self.calendar_notice,
            "calendar_source": self.calendar_source,
            "calendar_version": self.calendar_version,
            "calendar_verified": self.calendar_verified,
            "calendar_fingerprint": self.calendar_fingerprint,
            "expected_latest_date": self.expected_latest_date,
            "required_history_bars": self.required_history_bars,
            "expected_symbol_count": self.expected_symbol_count,
            "observed_symbol_count": self.observed_symbol_count,
            "healthy_symbol_count": self.healthy_symbol_count,
            "missing_symbols": list(self.missing_symbols),
            "unexpected_symbols": list(self.unexpected_symbols),
            "sources": list(self.sources),
            "mixed_source_symbols": list(self.mixed_source_symbols),
            "missing_source_symbols": list(self.missing_source_symbols),
            "source_consistent": self.source_consistent,
            "calendar_coverage": {
                "start": self.calendar_coverage_start,
                "end": self.calendar_coverage_end,
            },
            "max_missing_streak": self.max_missing_streak,
            "source_lineage": {
                "sources": list(self.sources),
                "mixed_source_symbols": list(self.mixed_source_symbols),
                "missing_source_symbols": list(self.missing_source_symbols),
                "source_consistent": self.source_consistent,
            },
            "cross_section": {
                "expected_date_symbol_count": self.expected_date_symbol_count,
                "expected_date_coverage_ratio": self.expected_date_coverage_ratio,
                "common_date_count": self.common_date_count,
                "latest_common_date": self.latest_common_date,
                "common_dates": list(self.common_dates),
            },
            "symbols": [item.to_dict() for item in self.symbols],
        }


class DailyBarDataQualityService:
    """Audit normalized daily bars without I/O, persistence or wall-clock reads.

    ``as_of`` is mandatory so the same input always produces the same output.
    The required window ends at :func:`expected_latest_daily_date`, including
    the project's 15:30 publication allowance.
    """

    def __init__(
        self,
        required_history_bars: int = 60,
        *,
        calendar: Optional[TradingCalendar] = None,
    ) -> None:
        if isinstance(required_history_bars, bool) or not isinstance(
            required_history_bars, int
        ):
            raise TypeError("required_history_bars must be an integer")
        required = required_history_bars
        if required <= 0:
            raise ValueError("required_history_bars must be positive")
        self.required_history_bars = required
        self.calendar = calendar or DEFAULT_TRADING_CALENDAR

    def audit(
        self,
        bars: Iterable[Bar],
        *,
        expected_symbols: Sequence[str] = (),
        as_of: datetime,
    ) -> DailyDataQualitySummary:
        audit_time = ensure_market_time(as_of)
        expected_latest = expected_latest_daily_date(
            audit_time, calendar=self.calendar
        )
        required_dates = self.calendar.sessions_ending_on(
            expected_latest, self.required_history_bars
        )
        required_date_set = set(required_dates)

        expected_set = {normalize_symbol(symbol) for symbol in expected_symbols}
        grouped: Dict[str, List[Bar]] = {}
        for bar in bars:
            if not isinstance(bar, Bar):
                raise TypeError("bars must contain normalized Bar objects")
            if bar.interval != "1d":
                raise ValueError("daily data quality audit only accepts interval '1d'")
            grouped.setdefault(bar.symbol, []).append(bar)

        observed_set = set(grouped)
        universe = expected_set | observed_set if expected_set else observed_set
        cross_section_universe = expected_set if expected_set else observed_set
        results: List[SymbolDailyQuality] = []
        usable_dates_by_symbol: Dict[str, set] = {}

        for symbol in sorted(universe):
            rows = grouped.get(symbol, [])
            dates = [bar.timestamp.date() for bar in rows]
            date_counts: Dict[date, int] = {}
            for item in dates:
                date_counts[item] = date_counts.get(item, 0) + 1

            unique_dates = set(date_counts)
            off_calendar_values = []
            out_of_coverage_values = []
            for item in sorted(unique_dates):
                try:
                    is_session = self.calendar.is_session(item)
                except CalendarRangeError:
                    # A finite calendar cannot infer an unknown weekday. Keep
                    # it visible as an explicit coverage problem rather than
                    # silently treating it as a holiday or dropping the row.
                    out_of_coverage_values.append(item)
                    continue
                if not is_session:
                    off_calendar_values.append(item)
            off_calendar = tuple(off_calendar_values)
            out_of_coverage = tuple(out_of_coverage_values)
            off_calendar_set = set(off_calendar)
            out_of_coverage_set = set(out_of_coverage)
            usable_dates = {
                item
                for item in unique_dates
                if item <= expected_latest
                and item not in off_calendar_set
                and item not in out_of_coverage_set
            }
            usable_dates_by_symbol[symbol] = usable_dates & required_date_set
            available = len(usable_dates_by_symbol[symbol])
            missing = tuple(sorted(required_date_set - usable_dates))
            # Express missingness in the calendar's session order.  Counting
            # date deltas would incorrectly treat exchange holidays as data
            # gaps.  Keep compact inclusive ranges for UI and diagnostics.
            missing_indices = sorted(
                required_dates.index(item) for item in missing
            )
            missing_streak = 0
            missing_ranges: List[str] = []
            if missing_indices:
                range_start = range_previous = missing_indices[0]
                for index in missing_indices[1:]:
                    if index == range_previous + 1:
                        range_previous = index
                        continue
                    missing_ranges.append(
                        "{}..{}".format(
                            required_dates[range_start].isoformat(),
                            required_dates[range_previous].isoformat(),
                        )
                    )
                    range_start = range_previous = index
                missing_ranges.append(
                    "{}..{}".format(
                        required_dates[range_start].isoformat(),
                        required_dates[range_previous].isoformat(),
                    )
                )
            # Recompute the longest run independently of range boundaries.
            longest_missing_streak = 0
            current_missing_streak = 0
            previous_index = None
            for index in missing_indices:
                if previous_index is not None and index == previous_index + 1:
                    current_missing_streak += 1
                else:
                    current_missing_streak = 1
                longest_missing_streak = max(longest_missing_streak, current_missing_streak)
                previous_index = index
            duplicates = tuple(sorted(item for item, count in date_counts.items() if count > 1))
            future = tuple(sorted(item for item in unique_dates if item > expected_latest))
            duplicate_rows = sum(date_counts[item] - 1 for item in duplicates)
            # Preserve the normalized domain value exactly.  In particular,
            # an empty source stays empty instead of being relabeled as a
            # seemingly trustworthy provider.
            sources = tuple(sorted({bar.source for bar in rows}))
            missing_source_rows = sum(not bar.source.strip() for bar in rows)
            source_consistent = len(sources) <= 1
            source_complete = missing_source_rows == 0

            # Count rows below the ingestion high-water mark.  Comparing only
            # adjacent rows would miss a late row that is newer than the
            # immediately preceding backfill but still older than an earlier
            # row in the same input batch.
            out_of_order = 0
            maximum_timestamp = None
            for bar in rows:
                if maximum_timestamp is not None and bar.timestamp < maximum_timestamp:
                    out_of_order += 1
                if maximum_timestamp is None or bar.timestamp > maximum_timestamp:
                    maximum_timestamp = bar.timestamp

            latest_usable = max(usable_dates) if usable_dates else None
            stale_sessions = (
                sum(1 for item in required_dates if item > latest_usable)
                if latest_usable is not None
                else self.required_history_bars
            )
            latest_expected_present = expected_latest in usable_dates
            shortfall = self.required_history_bars - available

            if not rows:
                status = "missing"
            elif shortfall > 0 or not latest_expected_present:
                status = "insufficient"
            elif (
                duplicate_rows
                or out_of_order
                or future
                or off_calendar
                or out_of_coverage
                or not source_consistent
                or not source_complete
            ):
                status = "warning"
            else:
                status = "ok"

            results.append(
                SymbolDailyQuality(
                    symbol=symbol,
                    status=status,
                    rows_received=len(rows),
                    unique_dates=len(unique_dates),
                    first_date=min(unique_dates).isoformat() if unique_dates else None,
                    latest_date=max(unique_dates).isoformat() if unique_dates else None,
                    latest_usable_date=latest_usable.isoformat() if latest_usable else None,
                    expected_latest_date=expected_latest.isoformat(),
                    latest_expected_present=latest_expected_present,
                    required_history_bars=self.required_history_bars,
                    available_history_bars=available,
                    history_shortfall=shortfall,
                    history_coverage_ratio=round(available / self.required_history_bars, 6),
                    stale_session_count=stale_sessions,
                    missing_date_count=len(missing),
                    missing_dates=tuple(item.isoformat() for item in missing),
                    duplicate_row_count=duplicate_rows,
                    duplicate_dates=tuple(item.isoformat() for item in duplicates),
                    out_of_order_count=out_of_order,
                    future_date_count=len(future),
                    future_dates=tuple(item.isoformat() for item in future),
                    off_calendar_date_count=len(off_calendar),
                    off_calendar_dates=tuple(item.isoformat() for item in off_calendar),
                    sources=sources,
                    source_consistent=source_consistent,
                    source_complete=source_complete,
                    missing_source_row_count=missing_source_rows,
                    max_missing_streak=longest_missing_streak,
                    missing_session_ranges=tuple(missing_ranges),
                    calendar_out_of_coverage_date_count=len(out_of_coverage),
                    calendar_out_of_coverage_dates=tuple(
                        item.isoformat() for item in out_of_coverage
                    ),
                )
            )

        if cross_section_universe:
            common = set(required_dates)
            for symbol in cross_section_universe:
                common &= usable_dates_by_symbol.get(symbol, set())
            expected_date_count = sum(
                expected_latest in usable_dates_by_symbol.get(symbol, set())
                for symbol in cross_section_universe
            )
            expected_date_ratio = round(
                expected_date_count / len(cross_section_universe), 6
            )
        else:
            common = set()
            expected_date_count = 0
            expected_date_ratio = 0.0

        missing_symbols = tuple(sorted(expected_set - observed_set))
        unexpected_symbols = tuple(sorted(observed_set - expected_set)) if expected_set else ()
        healthy_count = sum(item.healthy for item in results if item.symbol in cross_section_universe)
        overall_ok = bool(cross_section_universe) and all(
            item.healthy for item in results if item.symbol in cross_section_universe
        )
        if unexpected_symbols:
            overall_ok = False

        global_sources = tuple(
            sorted({source for item in results for source in item.sources})
        )
        mixed_source_symbols = tuple(
            item.symbol for item in results if not item.source_consistent
        )
        missing_source_symbols = tuple(
            item.symbol for item in results if not item.source_complete
        )
        # A cross section has consistent lineage only when every observed row
        # names one and the same non-empty source.  This catches both mixed
        # rows within a symbol and source drift between symbols.
        source_consistent = (
            bool(observed_set)
            and len(global_sources) == 1
            and global_sources[0].strip() != ""
            and not mixed_source_symbols
            and not missing_source_symbols
        )
        if not source_consistent:
            overall_ok = False

        common_ordered = tuple(sorted(common))
        calendar_coverage = self.calendar.coverage
        return DailyDataQualitySummary(
            schema_version=3,
            status="ok" if overall_ok else "degraded",
            as_of=audit_time.isoformat(),
            interval="1d",
            calendar=self.calendar.metadata.identifier,
            calendar_notice=self.calendar.metadata.notice,
            calendar_source=self.calendar.metadata.source,
            calendar_version=self.calendar.metadata.version,
            # Structural Protocol metadata is descriptive, not an official
            # provenance attestation.  Current public results remain unverified.
            calendar_verified=False,
            expected_latest_date=expected_latest.isoformat(),
            required_history_bars=self.required_history_bars,
            expected_symbol_count=len(cross_section_universe),
            observed_symbol_count=len(observed_set),
            healthy_symbol_count=healthy_count,
            missing_symbols=missing_symbols,
            unexpected_symbols=unexpected_symbols,
            expected_date_symbol_count=expected_date_count,
            expected_date_coverage_ratio=expected_date_ratio,
            common_date_count=len(common_ordered),
            latest_common_date=common_ordered[-1].isoformat() if common_ordered else None,
            common_dates=tuple(item.isoformat() for item in common_ordered),
            sources=global_sources,
            mixed_source_symbols=mixed_source_symbols,
            missing_source_symbols=missing_source_symbols,
            source_consistent=source_consistent,
            symbols=tuple(results),
            calendar_coverage_start=(
                calendar_coverage[0].isoformat() if calendar_coverage else None
            ),
            calendar_coverage_end=(
                calendar_coverage[1].isoformat() if calendar_coverage else None
            ),
            calendar_fingerprint=getattr(self.calendar, "fingerprint", None),
            max_missing_streak=max(
                (item.max_missing_streak for item in results), default=0
            ),
        )


__all__ = [
    "DailyBarDataQualityService",
    "DailyDataQualitySummary",
    "SymbolDailyQuality",
]
