from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence

from app.marketdata.bybit_funding import BybitFundingRateRecord

_ZERO = Decimal("0")


class CryptoFundingImpactStatus(StrEnum):
    COMPLETE = "COMPLETE"
    MISSING_MARK_PRICE = "MISSING_MARK_PRICE"
    FUNDING_BOUNDARY_AMBIGUOUS = "FUNDING_BOUNDARY_AMBIGUOUS"


@dataclass(frozen=True)
class CryptoFundingMarkSnapshot:
    symbol: str
    funding_time: datetime
    mark_price: Decimal
    source: str

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise ValueError("funding mark symbol must be normalized USDT symbol")
        if self.funding_time.tzinfo is None:
            raise ValueError("funding mark timestamp must be timezone-aware")
        if not self.mark_price.is_finite() or self.mark_price <= 0:
            raise ValueError("funding mark price must be positive and finite")
        if not self.source:
            raise ValueError("funding mark source cannot be empty")


@dataclass(frozen=True)
class CryptoFundingEventImpact:
    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal
    position_quantity: Decimal
    position_value_usdt: Decimal
    funding_pnl_usdt: Decimal
    mark_source: str


@dataclass(frozen=True)
class CryptoTradeFundingImpact:
    status: CryptoFundingImpactStatus
    symbol: str
    entry_time: datetime
    exit_time: datetime
    trade_net_before_funding_usdt: Decimal
    funding_pnl_usdt: Decimal | None
    trade_net_after_funding_usdt: Decimal | None
    event_count: int
    missing_mark_times: tuple[datetime, ...]
    ambiguous_boundary_times: tuple[datetime, ...]
    events: tuple[CryptoFundingEventImpact, ...]
    accounting_overlay_only: bool = True
    funding_feedback_into_position_sizing: bool = False
    funding_feedback_into_session_risk: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False


def calculate_trade_funding_impact(
    trade: Mapping[str, Any],
    *,
    funding_rates: Sequence[BybitFundingRateRecord],
    mark_snapshots: Sequence[CryptoFundingMarkSnapshot],
) -> CryptoTradeFundingImpact:
    """Calculate historical funding only when exact funding-time mark evidence is supplied.

    Positive ``funding_pnl_usdt`` means the position received funding; negative means it paid.
    A positive funding rate therefore debits a long and credits a short. Funding events exactly
    equal to entry/exit timestamps are left unresolved because this replay does not prove
    exchange sequencing at that boundary.
    """

    symbol = _text(trade, "symbol")
    side = _text(trade, "side")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("funding impact trade side must be LONG or SHORT")
    entry_time = _time(trade, "entry_time")
    exit_time = _time(trade, "exit_time")
    if exit_time <= entry_time:
        raise ValueError("funding impact exit_time must be after entry_time")
    quantity = _positive_decimal(trade, "quantity")
    trade_net = _decimal(trade, "net_pnl_usdt")

    rate_rows = tuple(
        sorted(
            (row for row in funding_rates if row.symbol == symbol),
            key=lambda row: row.funding_time,
        )
    )
    for row in rate_rows:
        row.validate()
    marks = _mark_index(mark_snapshots, symbol=symbol)

    ambiguous = tuple(
        row.funding_time
        for row in rate_rows
        if row.funding_time == entry_time or row.funding_time == exit_time
    )
    if ambiguous:
        return CryptoTradeFundingImpact(
            status=CryptoFundingImpactStatus.FUNDING_BOUNDARY_AMBIGUOUS,
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            trade_net_before_funding_usdt=trade_net,
            funding_pnl_usdt=None,
            trade_net_after_funding_usdt=None,
            event_count=0,
            missing_mark_times=(),
            ambiguous_boundary_times=ambiguous,
            events=(),
        )

    relevant = tuple(
        row for row in rate_rows if entry_time < row.funding_time < exit_time
    )
    missing = tuple(
        row.funding_time for row in relevant if row.funding_time not in marks
    )
    if missing:
        return CryptoTradeFundingImpact(
            status=CryptoFundingImpactStatus.MISSING_MARK_PRICE,
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            trade_net_before_funding_usdt=trade_net,
            funding_pnl_usdt=None,
            trade_net_after_funding_usdt=None,
            event_count=0,
            missing_mark_times=missing,
            ambiguous_boundary_times=(),
            events=(),
        )

    events: list[CryptoFundingEventImpact] = []
    total = _ZERO
    for row in relevant:
        mark = marks[row.funding_time]
        value = quantity * mark.mark_price
        raw_fee = value * row.funding_rate
        funding_pnl = -raw_fee if side == "LONG" else raw_fee
        total += funding_pnl
        events.append(
            CryptoFundingEventImpact(
                symbol=symbol,
                funding_time=row.funding_time,
                funding_rate=row.funding_rate,
                mark_price=mark.mark_price,
                position_quantity=quantity,
                position_value_usdt=value,
                funding_pnl_usdt=funding_pnl,
                mark_source=mark.source,
            )
        )
    return CryptoTradeFundingImpact(
        status=CryptoFundingImpactStatus.COMPLETE,
        symbol=symbol,
        entry_time=entry_time,
        exit_time=exit_time,
        trade_net_before_funding_usdt=trade_net,
        funding_pnl_usdt=total,
        trade_net_after_funding_usdt=trade_net + total,
        event_count=len(events),
        missing_mark_times=(),
        ambiguous_boundary_times=(),
        events=tuple(events),
    )


def _mark_index(
    snapshots: Sequence[CryptoFundingMarkSnapshot],
    *,
    symbol: str,
) -> dict[datetime, CryptoFundingMarkSnapshot]:
    result: dict[datetime, CryptoFundingMarkSnapshot] = {}
    for snapshot in snapshots:
        snapshot.validate()
        if snapshot.symbol != symbol:
            continue
        existing = result.get(snapshot.funding_time)
        if existing is not None and existing.mark_price != snapshot.mark_price:
            raise ValueError("conflicting funding mark prices for one timestamp")
        result[snapshot.funding_time] = snapshot
    return result


def _text(trade: Mapping[str, Any], field: str) -> str:
    value = trade.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"funding impact trade missing {field}")
    return value


def _time(trade: Mapping[str, Any], field: str) -> datetime:
    value = _text(trade, field)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"funding impact trade {field} must be timezone-aware")
    return parsed


def _decimal(trade: Mapping[str, Any], field: str) -> Decimal:
    value = trade.get(field)
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"funding impact trade {field} must be finite")
    return parsed


def _positive_decimal(trade: Mapping[str, Any], field: str) -> Decimal:
    parsed = _decimal(trade, field)
    if parsed <= 0:
        raise ValueError(f"funding impact trade {field} must be positive")
    return parsed
