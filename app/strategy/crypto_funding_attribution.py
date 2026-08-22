from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.marketdata.bybit_derivatives_history import BybitDerivativesHistory
from app.marketdata.bybit_mark_price_history import BybitMarkPriceHistory

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CryptoFundingSettlementAttribution:
    symbol: str
    side: str
    funding_timestamp_ms: int
    quantity: Decimal
    mark_price_usdt: Decimal
    position_value_usdt: Decimal
    funding_rate: Decimal
    funding_pnl_usdt: Decimal

    def validate(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("crypto funding attribution side is invalid")
        for name, value in (
            ("quantity", self.quantity),
            ("mark_price_usdt", self.mark_price_usdt),
            ("position_value_usdt", self.position_value_usdt),
            ("funding_rate", self.funding_rate),
            ("funding_pnl_usdt", self.funding_pnl_usdt),
        ):
            if not value.is_finite():
                raise ValueError(f"crypto funding attribution {name} must be finite")
        if self.quantity <= 0 or self.mark_price_usdt <= 0 or self.position_value_usdt <= 0:
            raise ValueError("crypto funding attribution quantity/price/value must be positive")
        if self.position_value_usdt != self.quantity * self.mark_price_usdt:
            raise ValueError("crypto funding attribution position value is inconsistent")
        expected = _funding_pnl(
            side=self.side,
            position_value_usdt=self.position_value_usdt,
            funding_rate=self.funding_rate,
        )
        if self.funding_pnl_usdt != expected:
            raise ValueError("crypto funding attribution PnL sign/economics are inconsistent")


@dataclass(frozen=True)
class CryptoTradeFundingAttribution:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    quantity: Decimal
    replay_net_pnl_usdt: Decimal
    funding_pnl_usdt: Decimal | None
    net_pnl_after_funding_usdt: Decimal | None
    settlement_count: int
    settlements: tuple[CryptoFundingSettlementAttribution, ...]
    complete: bool
    missing_reasons: tuple[str, ...]

    def validate(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("crypto trade funding side is invalid")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("crypto trade funding quantity must be positive and finite")
        if not self.replay_net_pnl_usdt.is_finite():
            raise ValueError("crypto trade funding replay net PnL must be finite")
        if self.settlement_count != len(self.settlements):
            raise ValueError("crypto trade funding settlement count is inconsistent")
        for settlement in self.settlements:
            settlement.validate()
            if settlement.symbol != self.symbol or settlement.side != self.side:
                raise ValueError("crypto trade funding settlement identity mismatch")
        if self.complete:
            if self.missing_reasons:
                raise ValueError("complete funding attribution cannot carry missing reasons")
            if self.funding_pnl_usdt is None or self.net_pnl_after_funding_usdt is None:
                raise ValueError("complete funding attribution requires dollar PnL")
            expected_funding = sum(
                (item.funding_pnl_usdt for item in self.settlements),
                start=_ZERO,
            )
            if self.funding_pnl_usdt != expected_funding:
                raise ValueError("crypto trade funding total does not match settlements")
            if self.net_pnl_after_funding_usdt != self.replay_net_pnl_usdt + expected_funding:
                raise ValueError("crypto trade funding adjusted PnL is inconsistent")
        else:
            if self.funding_pnl_usdt is not None or self.net_pnl_after_funding_usdt is not None:
                raise ValueError("incomplete funding attribution cannot claim dollar PnL")
            if not self.missing_reasons:
                raise ValueError("incomplete funding attribution requires missing reason")


def build_crypto_funding_attribution(
    replay: Mapping[str, Any],
    derivatives_histories: Mapping[str, BybitDerivativesHistory],
    mark_price_histories: Mapping[str, BybitMarkPriceHistory],
) -> tuple[CryptoTradeFundingAttribution, ...]:
    """Reconstruct funding PnL for replay trades from quantity and settlement mark price.

    For a positive funding rate LONG pays and SHORT receives. For a negative funding rate the sign
    naturally reverses. A funding event is counted only when `entry_time < timestamp <= exit_time`.
    Mark price must exist exactly at the settlement timestamp; no interpolation or future fill is
    allowed.
    """

    _validate_research_replay_boundary(replay)
    raw_trades = replay.get("closed_trades")
    if not isinstance(raw_trades, list):
        raise ValueError("crypto funding attribution requires replay closed_trades")
    result: list[CryptoTradeFundingAttribution] = []
    for raw in raw_trades:
        if not isinstance(raw, Mapping):
            raise ValueError("crypto funding attribution closed trade must be an object")
        symbol = _required_text(raw, "symbol")
        side = _required_text(raw, "side")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("crypto funding attribution replay side is invalid")
        entry_time = _required_text(raw, "entry_time")
        exit_time = _required_text(raw, "exit_time")
        entry_ms = _milliseconds(entry_time)
        exit_ms = _milliseconds(exit_time)
        if exit_ms < entry_ms:
            raise ValueError("crypto funding attribution exit precedes entry")
        quantity = _required_positive_decimal(raw, "quantity")
        replay_net = _required_decimal(raw, "net_pnl_usdt")
        derivatives = derivatives_histories.get(symbol)
        if derivatives is None:
            result.append(
                _incomplete_trade(
                    symbol=symbol,
                    side=side,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    quantity=quantity,
                    replay_net=replay_net,
                    reason="DERIVATIVES_HISTORY_MISSING",
                )
            )
            continue
        derivatives.validate()
        if derivatives.symbol != symbol:
            raise ValueError("crypto funding derivatives history identity mismatch")
        events = tuple(
            point
            for point in derivatives.funding
            if entry_ms < point.timestamp_ms <= exit_ms
        )
        if not events:
            trade = CryptoTradeFundingAttribution(
                symbol=symbol,
                side=side,
                entry_time=entry_time,
                exit_time=exit_time,
                quantity=quantity,
                replay_net_pnl_usdt=replay_net,
                funding_pnl_usdt=_ZERO,
                net_pnl_after_funding_usdt=replay_net,
                settlement_count=0,
                settlements=(),
                complete=True,
                missing_reasons=(),
            )
            trade.validate()
            result.append(trade)
            continue
        mark_history = mark_price_histories.get(symbol)
        if mark_history is None:
            result.append(
                _incomplete_trade(
                    symbol=symbol,
                    side=side,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    quantity=quantity,
                    replay_net=replay_net,
                    reason="MARK_PRICE_HISTORY_MISSING",
                )
            )
            continue
        mark_history.validate()
        if mark_history.symbol != symbol:
            raise ValueError("crypto funding mark-price history identity mismatch")
        settlements: list[CryptoFundingSettlementAttribution] = []
        missing: list[str] = []
        for event in events:
            mark_price = mark_history.open_price_at(event.timestamp_ms)
            if mark_price is None:
                missing.append(f"MARK_PRICE_AT_FUNDING_TIMESTAMP_MISSING:{event.timestamp_ms}")
                continue
            position_value = quantity * mark_price
            funding_pnl = _funding_pnl(
                side=side,
                position_value_usdt=position_value,
                funding_rate=event.funding_rate,
            )
            settlement = CryptoFundingSettlementAttribution(
                symbol=symbol,
                side=side,
                funding_timestamp_ms=event.timestamp_ms,
                quantity=quantity,
                mark_price_usdt=mark_price,
                position_value_usdt=position_value,
                funding_rate=event.funding_rate,
                funding_pnl_usdt=funding_pnl,
            )
            settlement.validate()
            settlements.append(settlement)
        if missing:
            trade = CryptoTradeFundingAttribution(
                symbol=symbol,
                side=side,
                entry_time=entry_time,
                exit_time=exit_time,
                quantity=quantity,
                replay_net_pnl_usdt=replay_net,
                funding_pnl_usdt=None,
                net_pnl_after_funding_usdt=None,
                settlement_count=len(settlements),
                settlements=tuple(settlements),
                complete=False,
                missing_reasons=tuple(missing),
            )
        else:
            total = sum((item.funding_pnl_usdt for item in settlements), start=_ZERO)
            trade = CryptoTradeFundingAttribution(
                symbol=symbol,
                side=side,
                entry_time=entry_time,
                exit_time=exit_time,
                quantity=quantity,
                replay_net_pnl_usdt=replay_net,
                funding_pnl_usdt=total,
                net_pnl_after_funding_usdt=replay_net + total,
                settlement_count=len(settlements),
                settlements=tuple(settlements),
                complete=True,
                missing_reasons=(),
            )
        trade.validate()
        result.append(trade)
    return tuple(result)


def diagnose_crypto_funding_attribution(
    trades: Sequence[CryptoTradeFundingAttribution],
) -> dict[str, Any]:
    records = tuple(trades)
    complete = tuple(item for item in records if item.complete)
    incomplete = tuple(item for item in records if not item.complete)
    trades_with_funding = tuple(item for item in complete if item.settlement_count > 0)
    funding_total = sum(
        (item.funding_pnl_usdt or _ZERO for item in complete),
        start=_ZERO,
    )
    replay_total = sum((item.replay_net_pnl_usdt for item in complete), start=_ZERO)
    adjusted_total = sum(
        (item.net_pnl_after_funding_usdt or item.replay_net_pnl_usdt for item in complete),
        start=_ZERO,
    )
    by_symbol: dict[str, list[CryptoTradeFundingAttribution]] = defaultdict(list)
    by_side: dict[str, list[CryptoTradeFundingAttribution]] = defaultdict(list)
    for item in complete:
        by_symbol[item.symbol].append(item)
        by_side[item.side].append(item)
    reconstruction_complete = len(incomplete) == 0
    return {
        "diagnostic": "BYBIT_REPLAY_FUNDING_DOLLAR_RECONSTRUCTION",
        "trade_count": len(records),
        "complete_trade_count": len(complete),
        "incomplete_trade_count": len(incomplete),
        "complete_fraction": (
            None
            if not records
            else float(Decimal(len(complete)) / Decimal(len(records)))
        ),
        "trades_crossing_funding": len(trades_with_funding),
        "settlement_count": sum(item.settlement_count for item in complete),
        "replay_net_pnl_usdt_complete_trades": float(replay_total),
        "reconstructed_funding_pnl_usdt": float(funding_total),
        "net_pnl_after_reconstructed_funding_usdt": float(adjusted_total),
        "by_symbol": {
            key: _funding_group_summary(values)
            for key, values in sorted(by_symbol.items())
        },
        "by_side": {
            key: _funding_group_summary(values)
            for key, values in sorted(by_side.items())
        },
        "incomplete_reasons": _missing_reason_counts(incomplete),
        "funding_formula": (
            "position_value=quantity*mark_price; "
            "funding_pnl=side_sign*position_value*rate"
        ),
        "mark_price_contract": (
            "uses Bybit mark-price candle open only when candle start exactly equals the funding "
            "settlement timestamp; no interpolation or future fill"
        ),
        "public_history_funding_reconstruction_complete": reconstruction_complete,
        "broker_ledger_funding_reconciled": False,
        "funding_dollar_cost_reconciled": False,
        "reconstruction_scope": "PUBLIC_BYBIT_FUNDING_AND_MARK_PRICE_HISTORY",
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _funding_group_summary(
    records: Sequence[CryptoTradeFundingAttribution],
) -> dict[str, Any]:
    funding = sum((item.funding_pnl_usdt or _ZERO for item in records), start=_ZERO)
    replay = sum((item.replay_net_pnl_usdt for item in records), start=_ZERO)
    adjusted = sum(
        (item.net_pnl_after_funding_usdt or item.replay_net_pnl_usdt for item in records),
        start=_ZERO,
    )
    return {
        "trade_count": len(records),
        "trades_crossing_funding": sum(item.settlement_count > 0 for item in records),
        "settlement_count": sum(item.settlement_count for item in records),
        "replay_net_pnl_usdt": float(replay),
        "funding_pnl_usdt": float(funding),
        "net_pnl_after_funding_usdt": float(adjusted),
    }


def _missing_reason_counts(
    records: Sequence[CryptoTradeFundingAttribution],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in records:
        for reason in item.missing_reasons:
            counts[reason] += 1
    return dict(sorted(counts.items()))


def _incomplete_trade(
    *,
    symbol: str,
    side: str,
    entry_time: str,
    exit_time: str,
    quantity: Decimal,
    replay_net: Decimal,
    reason: str,
) -> CryptoTradeFundingAttribution:
    trade = CryptoTradeFundingAttribution(
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        exit_time=exit_time,
        quantity=quantity,
        replay_net_pnl_usdt=replay_net,
        funding_pnl_usdt=None,
        net_pnl_after_funding_usdt=None,
        settlement_count=0,
        settlements=(),
        complete=False,
        missing_reasons=(reason,),
    )
    trade.validate()
    return trade


def _funding_pnl(
    *,
    side: str,
    position_value_usdt: Decimal,
    funding_rate: Decimal,
) -> Decimal:
    if side == "LONG":
        return -(position_value_usdt * funding_rate)
    if side == "SHORT":
        return position_value_usdt * funding_rate
    raise ValueError("crypto funding attribution side is invalid")


def _validate_research_replay_boundary(replay: Mapping[str, Any]) -> None:
    for field in ("strategy_promotion_allowed", "bybit_live_order_routing_allowed"):
        if replay.get(field) is not False:
            raise ValueError(
                f"crypto funding attribution rejected replay without explicit {field}=false"
            )


def _milliseconds(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("crypto funding attribution timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("crypto funding attribution timestamp must be timezone-aware")
    return int(parsed.timestamp() * 1000)


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"crypto funding attribution missing {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"crypto funding attribution missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"crypto funding attribution invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"crypto funding attribution non-finite {field}")
    return parsed


def _required_positive_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    parsed = _required_decimal(row, field)
    if parsed <= 0:
        raise ValueError(f"crypto funding attribution {field} must be positive")
    return parsed
