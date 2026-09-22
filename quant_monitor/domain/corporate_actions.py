"""Immutable company-action and adjustment-factor contracts.

The market providers currently expose raw, unadjusted daily bars only.  This
module deliberately does not fetch or infer corporate actions.  It gives the
storage and research layers a typed, auditable contract for adding an action
or a provider-computed adjustment factor later, without ever overwriting a
raw :class:`~quant_monitor.domain.models.Bar`.

``AdjustmentFactor`` is a price multiplier supplied by a trusted, versioned
factor set.  It is not calculated from a single price gap: factor generation
must happen in a provider/ingestion layer with the relevant exchange rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from enum import Enum
from typing import Any, Mapping, Optional

from .lineage import Adjustment
from .models import Bar, ensure_market_time, normalize_symbol


class CorporateActionType(str, Enum):
    """Stable action kinds understood by the research contract.

    A string enum keeps wire values stable while allowing future vendor action
    kinds to be quarantined without changing the persisted schema.  The
    constants are intentionally explicit and are validated by the model.
    """

    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    STOCK_SPLIT = "stock_split"
    REVERSE_SPLIT = "reverse_split"
    RIGHTS_ISSUE = "rights_issue"


_ACTION_TYPES = frozenset(
    {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.STOCK_DIVIDEND,
        CorporateActionType.STOCK_SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.RIGHTS_ISSUE,
    }
)


def _required_text(value: Any, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("{} cannot be empty".format(name))
    if "\x00" in text:
        raise ValueError("{} cannot contain NUL".format(name))
    return text


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash(value: Any, name: str) -> str:
    result = _required_text(value, name).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError("{} must be a lowercase SHA-256 hex digest".format(name))
    return result


def _decimal(value: Any, name: str, *, positive: bool = False) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        comparator = "positive" if positive else "non-negative"
        raise ValueError("{} must be {}".format(name, comparator))
    return result


def _decimal_signed(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    return result


def _metadata(value: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    # A shallow immutable mapping mirrors the rest of the lineage contract;
    # nested values remain provider-owned JSON and are never interpreted here.
    return MappingProxyType(dict(value.items()))


@dataclass(frozen=True)
class CorporateAction:
    """A versioned, point-in-time corporate action announcement.

    ``share_ratio`` means *new shares per old share*.  For example, a 10%
    stock dividend is ``0.10`` additional shares, a 2-for-1 split is ``2`` and
    a 1-for-2 reverse split is ``0.5``.  The field is intentionally absent for
    a cash dividend.  ``cash_amount`` is cash per old share before tax; tax
    treatment belongs to a separately versioned return model.
    """

    action_id: str
    symbol: str
    action_type: str
    ex_date: date
    announcement_date: Optional[date] = None
    record_date: Optional[date] = None
    payable_date: Optional[date] = None
    cash_amount: Optional[Decimal] = None
    share_ratio: Optional[Decimal] = None
    subscription_price: Optional[Decimal] = None
    source: str = "unknown"
    revision_id: str = ""
    received_at: datetime = field(default=None)  # type: ignore[assignment]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _required_text(self.action_id, "action_id"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        try:
            action_type = (
                self.action_type
                if isinstance(self.action_type, CorporateActionType)
                else CorporateActionType(str(self.action_type).strip().lower())
            )
        except ValueError as exc:
            raise ValueError(
                "unsupported corporate action type: {}".format(self.action_type)
            ) from exc
        object.__setattr__(self, "action_type", action_type)
        for name in ("ex_date", "announcement_date", "record_date", "payable_date"):
            value = getattr(self, name)
            if value is None and name == "ex_date":
                raise TypeError("ex_date must be a date")
            if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
                raise TypeError("{} must be a date".format(name))
            if value is not None:
                object.__setattr__(self, name, value)
        if self.announcement_date and self.announcement_date > self.ex_date:
            raise ValueError("announcement_date cannot follow ex_date")
        if (
            self.record_date is not None
            and self.announcement_date is not None
            and self.record_date < self.announcement_date
        ):
            raise ValueError("record_date cannot precede announcement_date")
        if self.payable_date and self.payable_date < self.ex_date:
            raise ValueError("payable_date cannot precede ex_date")
        cash_amount = _decimal(
            self.cash_amount,
            "cash_amount",
            positive=action_type == CorporateActionType.CASH_DIVIDEND,
        )
        share_ratio = _decimal(self.share_ratio, "share_ratio", positive=True)
        subscription_price = _decimal(self.subscription_price, "subscription_price", positive=True)
        if action_type == CorporateActionType.CASH_DIVIDEND and cash_amount is None:
            raise ValueError("cash_dividend requires cash_amount")
        if action_type == CorporateActionType.CASH_DIVIDEND and share_ratio is not None:
            raise ValueError("cash_dividend cannot include share_ratio")
        if action_type in {
            CorporateActionType.STOCK_DIVIDEND,
            CorporateActionType.STOCK_SPLIT,
            CorporateActionType.REVERSE_SPLIT,
        } and share_ratio is None:
            raise ValueError("share action requires share_ratio")
        if action_type == CorporateActionType.STOCK_SPLIT and share_ratio is not None and share_ratio <= 1:
            raise ValueError("stock_split share_ratio must be greater than 1")
        if action_type == CorporateActionType.REVERSE_SPLIT and share_ratio is not None and share_ratio >= 1:
            raise ValueError("reverse_split share_ratio must be below 1")
        if action_type != CorporateActionType.RIGHTS_ISSUE and subscription_price is not None:
            raise ValueError("subscription_price is only valid for rights_issue")
        if action_type == CorporateActionType.RIGHTS_ISSUE and (
            share_ratio is None or subscription_price is None
        ):
            raise ValueError("rights_issue requires share_ratio and subscription_price")
        if action_type not in {CorporateActionType.CASH_DIVIDEND, CorporateActionType.RIGHTS_ISSUE} and cash_amount is not None:
            raise ValueError("cash_amount is only valid for cash_dividend or rights_issue")
        if self.received_at is None:
            raise ValueError("received_at is required for an auditable action")
        received_at = ensure_market_time(self.received_at)
        object.__setattr__(self, "cash_amount", cash_amount)
        object.__setattr__(self, "share_ratio", share_ratio)
        object.__setattr__(self, "subscription_price", subscription_price)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "revision_id", _required_text(self.revision_id, "revision_id"))
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    @property
    def content_hash(self) -> str:
        """Deterministic hash excluding mutable metadata and receipt time."""

        return _sha256(
            {
                "action_id": self.action_id,
                "action_type": self.action_type.value,
                "announcement_date": self.announcement_date.isoformat() if self.announcement_date else None,
                "cash_amount": str(self.cash_amount) if self.cash_amount is not None else None,
                "ex_date": self.ex_date.isoformat(),
                "payable_date": self.payable_date.isoformat() if self.payable_date else None,
                "record_date": self.record_date.isoformat() if self.record_date else None,
                "revision_id": self.revision_id,
                "share_ratio": str(self.share_ratio) if self.share_ratio is not None else None,
                "source": self.source,
                "subscription_price": str(self.subscription_price) if self.subscription_price is not None else None,
                "symbol": self.symbol,
            }
        )


@dataclass(frozen=True)
class AdjustmentFactor:
    """A versioned multiplier for one symbol/session and one price basis."""

    factor_id: str
    symbol: str
    adjustment: Adjustment
    session_date: date
    benchmark_date: date
    factor: Decimal
    factor_version: str
    source: str
    revision_id: str
    input_hash: str
    computed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "factor_id", _required_text(self.factor_id, "factor_id"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        adjustment = self.adjustment if isinstance(self.adjustment, Adjustment) else Adjustment(self.adjustment)
        if adjustment == Adjustment.RAW and Decimal(str(self.factor)) != Decimal("1"):
            raise ValueError("raw adjustment factor must equal 1")
        object.__setattr__(self, "adjustment", adjustment)
        for name in ("session_date", "benchmark_date"):
            value = getattr(self, name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise TypeError("{} must be a date".format(name))
        factor = _decimal(self.factor, "factor", positive=True)
        assert factor is not None
        object.__setattr__(self, "factor", factor)
        object.__setattr__(self, "factor_version", _required_text(self.factor_version, "factor_version"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "revision_id", _required_text(self.revision_id, "revision_id"))
        object.__setattr__(self, "input_hash", _hash(self.input_hash, "input_hash"))
        object.__setattr__(self, "computed_at", ensure_market_time(self.computed_at))

    def apply_price(self, price: Any) -> Decimal:
        value = _decimal(price, "price")
        assert value is not None
        return value * self.factor

    @property
    def content_hash(self) -> str:
        """Stable hash of the factor's economic identity.

        ``computed_at`` is receipt/processing metadata and is deliberately
        excluded, so retrying the same immutable factor with a different local
        timestamp remains idempotent.  The source, revision and input hash are
        included because changing any of them changes the provenance binding.
        """

        return _sha256(
            {
                "factor_id": self.factor_id,
                "symbol": self.symbol,
                "adjustment": self.adjustment.value,
                "session_date": self.session_date.isoformat(),
                "benchmark_date": self.benchmark_date.isoformat(),
                "factor": str(self.factor),
                "factor_version": self.factor_version,
                "source": self.source,
                "revision_id": self.revision_id,
                "input_hash": self.input_hash,
            }
        )

    def apply_bar(self, bar: Bar) -> "AdjustedBar":
        # Keep validation at the public view's constructor so callers cannot
        # bypass identity checks by constructing AdjustedBar directly.
        return AdjustedBar(raw_bar=bar, factor_record=self)


@dataclass(frozen=True)
class AdjustedBar:
    """Explicit adjusted view; the original raw bar remains available."""

    raw_bar: Bar
    factor_record: AdjustmentFactor

    def __post_init__(self) -> None:
        if not isinstance(self.raw_bar, Bar):
            raise TypeError("raw_bar must be a Bar")
        if not isinstance(self.factor_record, AdjustmentFactor):
            raise TypeError("factor_record must be an AdjustmentFactor")
        if self.raw_bar.symbol != self.factor_record.symbol:
            raise ValueError("bar symbol does not match adjustment factor")
        if self.raw_bar.timestamp.date() != self.factor_record.session_date:
            raise ValueError("bar date does not match adjustment factor session_date")
        # to_bar() deliberately leaves a reserved provenance suffix.  Feeding
        # that materialized price back into a factor would otherwise silently
        # compound adjustments (even if a different version is supplied).
        # This detects our own views only; an arbitrary untagged vendor Bar
        # still needs independently verified raw-price lineage.
        source_parts = self.raw_bar.source.rsplit("|", 2)
        if len(source_parts) == 3 and source_parts[1] in {
            adjustment.value for adjustment in Adjustment
        }:
            raise ValueError(
                "bar is already a materialized adjustment view; use its original raw_bar"
            )

    @property
    def adjustment(self) -> Adjustment:
        return self.factor_record.adjustment

    @property
    def factor(self) -> Decimal:
        return self.factor_record.factor

    @property
    def open(self) -> Decimal:
        return self.factor_record.apply_price(self.raw_bar.open)

    @property
    def high(self) -> Decimal:
        return self.factor_record.apply_price(self.raw_bar.high)

    @property
    def low(self) -> Decimal:
        return self.factor_record.apply_price(self.raw_bar.low)

    @property
    def close(self) -> Decimal:
        return self.factor_record.apply_price(self.raw_bar.close)

    def to_bar(self) -> Bar:
        """Materialize a Bar only through an explicit, auditable opt-in."""

        return Bar(
            symbol=self.raw_bar.symbol,
            interval=self.raw_bar.interval,
            timestamp=self.raw_bar.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.raw_bar.volume,
            # Turnover/volume are deliberately left raw.  A split-aware
            # quantity contract is a separate concern and must not be implied
            # by a price-only factor.
            turnover=self.raw_bar.turnover,
            source="{}|{}|{}".format(
                self.raw_bar.source,
                self.adjustment.value,
                self.factor_record.factor_version,
            ),
        )


@dataclass(frozen=True)
class CorporateActionEffect:
    """Deterministic holding-ledger result for one corporate action.

    Quantities and cash remain :class:`Decimal` because a stock dividend or
    rights entitlement may be fractional before exchange-specific rounding.
    Lot-size and tax policy belong to the execution/return model and are not
    silently inferred here.
    """

    action_id: str
    symbol: str
    ex_date: date
    old_quantity: Decimal
    new_quantity: Decimal
    old_cash: Decimal
    new_cash: Decimal
    cash_delta: Decimal
    rights_entitlement: Decimal
    subscribed_rights: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _required_text(self.action_id, "action_id"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not isinstance(self.ex_date, date) or isinstance(self.ex_date, datetime):
            raise TypeError("ex_date must be a date")
        for name in (
            "old_quantity",
            "new_quantity",
            "old_cash",
            "new_cash",
            "cash_delta",
            "rights_entitlement",
        ):
            value = _decimal_signed(getattr(self, name), name) if name == "cash_delta" else _decimal(getattr(self, name), name)
            assert value is not None
            object.__setattr__(self, name, value)
        if self.new_quantity < 0 or self.new_cash < 0:
            raise ValueError("resulting quantity and cash cannot be negative")
        if self.new_cash - self.old_cash != self.cash_delta:
            raise ValueError("cash_delta must equal new_cash - old_cash")
        if not isinstance(self.subscribed_rights, bool):
            raise TypeError("subscribed_rights must be a boolean")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "action_id": self.action_id,
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "old_quantity": str(self.old_quantity),
            "new_quantity": str(self.new_quantity),
            "old_cash": str(self.old_cash),
            "new_cash": str(self.new_cash),
            "cash_delta": str(self.cash_delta),
            "rights_entitlement": str(self.rights_entitlement),
            "subscribed_rights": self.subscribed_rights,
        }


def apply_corporate_action(
    action: CorporateAction,
    *,
    quantity: Decimal,
    cash: Decimal,
    subscribe_rights: bool = False,
) -> CorporateActionEffect:
    """Apply one explicit action to a holding and cash balance.

    Cash dividends credit ``quantity * cash_amount``.  Stock dividends use a
    multiplier of ``1 + share_ratio``; splits and reverse splits use the
    stated total multiplier.  Rights issues are opt-in: unsubscribed rights
    leave the holding unchanged, while subscribing debits the exact stated
    subscription cost and fails if cash is insufficient.
    """

    if not isinstance(action, CorporateAction):
        raise TypeError("action must be a CorporateAction")
    old_quantity = _decimal(quantity, "quantity")
    old_cash = _decimal(cash, "cash")
    assert old_quantity is not None and old_cash is not None
    if not isinstance(subscribe_rights, bool):
        raise TypeError("subscribe_rights must be a boolean")
    new_quantity = old_quantity
    new_cash = old_cash
    rights_entitlement = Decimal("0")
    action_type = action.action_type
    if action_type == CorporateActionType.CASH_DIVIDEND:
        assert action.cash_amount is not None
        new_cash += old_quantity * action.cash_amount
    elif action_type == CorporateActionType.STOCK_DIVIDEND:
        assert action.share_ratio is not None
        new_quantity = old_quantity * (Decimal("1") + action.share_ratio)
    elif action_type in {
        CorporateActionType.STOCK_SPLIT,
        CorporateActionType.REVERSE_SPLIT,
    }:
        assert action.share_ratio is not None
        new_quantity = old_quantity * action.share_ratio
    elif action_type == CorporateActionType.RIGHTS_ISSUE:
        assert action.share_ratio is not None and action.subscription_price is not None
        rights_entitlement = old_quantity * action.share_ratio
        if subscribe_rights:
            cost = rights_entitlement * action.subscription_price
            if cost > old_cash:
                raise ValueError("insufficient cash to subscribe rights issue")
            new_quantity += rights_entitlement
            new_cash -= cost
    return CorporateActionEffect(
        action_id=action.action_id,
        symbol=action.symbol,
        ex_date=action.ex_date,
        old_quantity=old_quantity,
        new_quantity=new_quantity,
        old_cash=old_cash,
        new_cash=new_cash,
        cash_delta=new_cash - old_cash,
        rights_entitlement=rights_entitlement,
        subscribed_rights=subscribe_rights if action_type == CorporateActionType.RIGHTS_ISSUE else False,
    )


__all__ = [
    "AdjustedBar",
    "AdjustmentFactor",
    "CorporateAction",
    "CorporateActionType",
    "CorporateActionEffect",
    "apply_corporate_action",
]
