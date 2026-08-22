from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

from app.marketdata.bybit_derivatives_history import BybitDerivativesHistory

_ZERO = Decimal("0")
_LONG_HEAVY = Decimal("0.55")
_SHORT_HEAVY = Decimal("0.45")
TPoint = TypeVar("TPoint", bound="_TimestampPoint")


class _TimestampPoint(Protocol):
    timestamp_ms: int


@dataclass(frozen=True)
class CryptoTradeDerivativesContext:
    symbol: str
    side: str
    decision_time: str
    entry_time: str
    exit_time: str
    exit_reason: str
    net_pnl_usdt: Decimal
    maximum_favorable_r: Decimal
    maximum_adverse_r: Decimal
    open_interest_timestamp_ms: int | None
    open_interest: Decimal | None
    previous_open_interest: Decimal | None
    open_interest_delta: Decimal | None
    open_interest_delta_fraction: Decimal | None
    account_ratio_timestamp_ms: int | None
    long_account_ratio: Decimal | None
    short_account_ratio: Decimal | None
    long_short_account_ratio: Decimal | None
    prior_funding_timestamp_ms: int | None
    prior_funding_rate: Decimal | None
    holding_funding_rate_sum: Decimal
    holding_funding_event_count: int
    decision_context_complete: bool
    missing_reasons: tuple[str, ...]

    @property
    def open_interest_regime(self) -> str:
        if self.open_interest_delta is None:
            return "OI_UNKNOWN"
        if self.open_interest_delta > 0:
            return "OI_RISING"
        if self.open_interest_delta < 0:
            return "OI_FALLING"
        return "OI_FLAT"

    @property
    def crowding_regime(self) -> str:
        if self.long_account_ratio is None:
            return "CROWDING_UNKNOWN"
        if self.long_account_ratio >= _LONG_HEAVY:
            return "LONG_HEAVY"
        if self.long_account_ratio <= _SHORT_HEAVY:
            return "SHORT_HEAVY"
        return "BALANCED"

    @property
    def prior_funding_regime(self) -> str:
        if self.prior_funding_rate is None:
            return "FUNDING_UNKNOWN"
        if self.prior_funding_rate > 0:
            return "FUNDING_POSITIVE"
        if self.prior_funding_rate < 0:
            return "FUNDING_NEGATIVE"
        return "FUNDING_ZERO"

    @property
    def repeated_pattern(self) -> str:
        return "|".join(
            (
                self.side,
                self.open_interest_regime,
                self.crowding_regime,
                self.prior_funding_regime,
            )
        )


def build_crypto_trade_derivatives_context(
    replay: Mapping[str, Any],
    histories: Mapping[str, BybitDerivativesHistory],
) -> tuple[CryptoTradeDerivativesContext, ...]:
    """Join only point-in-time-known derivatives data to each closed replay trade.

    OI, account ratio and prior funding are selected at or before the decision timestamp. Funding
    observed after entry is kept in a separate holding-period attribution field and is never used as
    a pre-entry feature.
    """

    _validate_research_replay_boundary(replay)
    raw_trades = replay.get("closed_trades")
    raw_events = replay.get("decision_events")
    if not isinstance(raw_trades, list) or not isinstance(raw_events, list):
        raise ValueError("derivatives context requires replay closed_trades and decision_events")
    entry_events = _entry_events(raw_events)
    contexts: list[CryptoTradeDerivativesContext] = []
    for raw in raw_trades:
        if not isinstance(raw, Mapping):
            raise ValueError("derivatives context closed trade must be an object")
        symbol = _required_text(raw, "symbol")
        entry_time = _required_text(raw, "entry_time")
        event = entry_events.get((symbol, entry_time))
        if event is None:
            raise ValueError("derivatives context cannot match closed trade to ENTRY event")
        side = _required_text(raw, "side")
        event_side = _required_text(event, "side")
        if event_side != side:
            raise ValueError("derivatives context ENTRY side differs from closed trade")
        decision_time = _required_text(raw, "decision_time")
        event_decision = _required_text(event, "decision_time")
        if event_decision != decision_time:
            raise ValueError("derivatives context decision timestamp mismatch")
        exit_time = _required_text(raw, "exit_time")
        decision_ms = _milliseconds(decision_time)
        entry_ms = _milliseconds(entry_time)
        exit_ms = _milliseconds(exit_time)
        if not decision_ms < entry_ms <= exit_ms:
            raise ValueError("derivatives context trade timestamps are not monotonic")
        history = histories.get(symbol)
        if history is None:
            contexts.append(
                _missing_history_context(
                    raw,
                    symbol=symbol,
                    side=side,
                    decision_time=decision_time,
                    entry_time=entry_time,
                    exit_time=exit_time,
                )
            )
            continue
        history.validate()
        if history.symbol != symbol:
            raise ValueError("derivatives context history key/symbol mismatch")
        if not history.start_ms <= decision_ms <= history.end_ms:
            raise ValueError("derivatives context decision falls outside acquired history")
        if exit_ms > history.end_ms:
            raise ValueError("derivatives context exit falls outside acquired history")

        current_oi = _latest_at_or_before(history.open_interest, decision_ms)
        previous_oi = (
            None
            if current_oi is None
            else _latest_before(history.open_interest, current_oi.timestamp_ms)
        )
        ratio = _latest_at_or_before(history.account_ratio, decision_ms)
        prior_funding = _latest_at_or_before(history.funding, decision_ms)
        holding_funding = tuple(
            point
            for point in history.funding
            if entry_ms < point.timestamp_ms <= exit_ms
        )
        missing: list[str] = []
        if current_oi is None:
            missing.append("OPEN_INTEREST_AT_DECISION_MISSING")
        if previous_oi is None:
            missing.append("OPEN_INTEREST_PREVIOUS_POINT_MISSING")
        if ratio is None:
            missing.append("ACCOUNT_RATIO_AT_DECISION_MISSING")
        if prior_funding is None:
            missing.append("PRIOR_FUNDING_RATE_MISSING")

        oi_delta: Decimal | None = None
        oi_delta_fraction: Decimal | None = None
        if current_oi is not None and previous_oi is not None:
            oi_delta = current_oi.open_interest - previous_oi.open_interest
            if previous_oi.open_interest > 0:
                oi_delta_fraction = oi_delta / previous_oi.open_interest
            else:
                missing.append("PREVIOUS_OPEN_INTEREST_ZERO")

        context = CryptoTradeDerivativesContext(
            symbol=symbol,
            side=side,
            decision_time=decision_time,
            entry_time=entry_time,
            exit_time=exit_time,
            exit_reason=_required_text(raw, "exit_reason"),
            net_pnl_usdt=_required_decimal(raw, "net_pnl_usdt"),
            maximum_favorable_r=_required_decimal(
                raw, "maximum_favorable_r_before_exit"
            ),
            maximum_adverse_r=_required_decimal(
                raw, "maximum_adverse_r_before_exit"
            ),
            open_interest_timestamp_ms=(
                None if current_oi is None else current_oi.timestamp_ms
            ),
            open_interest=(None if current_oi is None else current_oi.open_interest),
            previous_open_interest=(
                None if previous_oi is None else previous_oi.open_interest
            ),
            open_interest_delta=oi_delta,
            open_interest_delta_fraction=oi_delta_fraction,
            account_ratio_timestamp_ms=(None if ratio is None else ratio.timestamp_ms),
            long_account_ratio=(None if ratio is None else ratio.buy_ratio),
            short_account_ratio=(None if ratio is None else ratio.sell_ratio),
            long_short_account_ratio=(
                None if ratio is None else ratio.long_short_ratio
            ),
            prior_funding_timestamp_ms=(
                None if prior_funding is None else prior_funding.timestamp_ms
            ),
            prior_funding_rate=(
                None if prior_funding is None else prior_funding.funding_rate
            ),
            holding_funding_rate_sum=sum(
                (point.funding_rate for point in holding_funding),
                start=_ZERO,
            ),
            holding_funding_event_count=len(holding_funding),
            decision_context_complete=not missing,
            missing_reasons=tuple(dict.fromkeys(missing)),
        )
        _validate_context_no_lookahead(context, decision_ms=decision_ms)
        contexts.append(context)
    return tuple(contexts)


def diagnose_crypto_derivatives_context(
    contexts: Sequence[CryptoTradeDerivativesContext],
    *,
    minimum_pattern_trades: int = 5,
) -> dict[str, Any]:
    if not 1 <= minimum_pattern_trades <= 10_000:
        raise ValueError("derivatives context minimum_pattern_trades is invalid")
    records = tuple(contexts)
    complete = tuple(item for item in records if item.decision_context_complete)
    by_symbol = _group_summary(records, key=lambda item: item.symbol)
    by_side = _group_summary(records, key=lambda item: item.side)
    by_oi = _group_summary(records, key=lambda item: item.open_interest_regime)
    by_crowding = _group_summary(records, key=lambda item: item.crowding_regime)
    by_prior_funding = _group_summary(records, key=lambda item: item.prior_funding_regime)
    patterns = _pattern_summary(records, minimum_pattern_trades=minimum_pattern_trades)
    return {
        "diagnostic": "BYBIT_CRYPTO_POINT_IN_TIME_DERIVATIVES_CONTEXT",
        "trade_count": len(records),
        "complete_decision_context_count": len(complete),
        "complete_decision_context_fraction": (
            None
            if not records
            else float(Decimal(len(complete)) / Decimal(len(records)))
        ),
        "aggregate": _summary(records),
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_open_interest_regime": by_oi,
        "by_crowding_regime": by_crowding,
        "by_prior_funding_regime": by_prior_funding,
        "repeated_patterns": patterns,
        "pre_entry_timing_contract": (
            "open interest, account ratio and prior funding use only points with timestamp <= "
            "decision_time"
        ),
        "holding_funding_contract": (
            "holding_funding_rate_sum includes only settled funding timestamps strictly after "
            "entry and at or before exit; it is post-entry attribution, not an entry feature"
        ),
        "funding_dollar_cost_reconciled": False,
        "interpretation_contract": (
            "derivatives context is historical association evidence; no OI/crowding/funding state "
            "is treated as a causal guarantee of profit"
        ),
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _missing_history_context(
    raw: Mapping[str, Any],
    *,
    symbol: str,
    side: str,
    decision_time: str,
    entry_time: str,
    exit_time: str,
) -> CryptoTradeDerivativesContext:
    return CryptoTradeDerivativesContext(
        symbol=symbol,
        side=side,
        decision_time=decision_time,
        entry_time=entry_time,
        exit_time=exit_time,
        exit_reason=_required_text(raw, "exit_reason"),
        net_pnl_usdt=_required_decimal(raw, "net_pnl_usdt"),
        maximum_favorable_r=_required_decimal(
            raw, "maximum_favorable_r_before_exit"
        ),
        maximum_adverse_r=_required_decimal(
            raw, "maximum_adverse_r_before_exit"
        ),
        open_interest_timestamp_ms=None,
        open_interest=None,
        previous_open_interest=None,
        open_interest_delta=None,
        open_interest_delta_fraction=None,
        account_ratio_timestamp_ms=None,
        long_account_ratio=None,
        short_account_ratio=None,
        long_short_account_ratio=None,
        prior_funding_timestamp_ms=None,
        prior_funding_rate=None,
        holding_funding_rate_sum=_ZERO,
        holding_funding_event_count=0,
        decision_context_complete=False,
        missing_reasons=("DERIVATIVES_HISTORY_MISSING",),
    )


def _validate_context_no_lookahead(
    context: CryptoTradeDerivativesContext,
    *,
    decision_ms: int,
) -> None:
    for name, timestamp in (
        ("open interest", context.open_interest_timestamp_ms),
        ("account ratio", context.account_ratio_timestamp_ms),
        ("prior funding", context.prior_funding_timestamp_ms),
    ):
        if timestamp is not None and timestamp > decision_ms:
            raise ValueError(f"derivatives context {name} contains lookahead")


def _entry_events(events: Sequence[Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("event") != "ENTRY":
            continue
        symbol = _required_text(event, "symbol")
        entry_time = _required_text(event, "execution_time")
        key = (symbol, entry_time)
        if key in result:
            raise ValueError("derivatives context replay has duplicate ENTRY event key")
        result[key] = event
    return result


def _latest_at_or_before(points: Sequence[TPoint], timestamp_ms: int) -> TPoint | None:
    timestamps = [point.timestamp_ms for point in points]
    index = bisect_right(timestamps, timestamp_ms) - 1
    return None if index < 0 else points[index]


def _latest_before(points: Sequence[TPoint], timestamp_ms: int) -> TPoint | None:
    timestamps = [point.timestamp_ms for point in points]
    index = bisect_right(timestamps, timestamp_ms - 1) - 1
    return None if index < 0 else points[index]


def _group_summary(
    records: Sequence[CryptoTradeDerivativesContext],
    *,
    key: Any,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[CryptoTradeDerivativesContext]] = defaultdict(list)
    for record in records:
        grouped[str(key(record))].append(record)
    return {
        group: _summary(members)
        for group, members in sorted(grouped.items())
    }


def _pattern_summary(
    records: Sequence[CryptoTradeDerivativesContext],
    *,
    minimum_pattern_trades: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[CryptoTradeDerivativesContext]] = defaultdict(list)
    for record in records:
        grouped[record.repeated_pattern].append(record)
    result: list[dict[str, Any]] = []
    for pattern, members in grouped.items():
        item = _summary(members)
        item["pattern"] = pattern
        item["sample_sufficient"] = len(members) >= minimum_pattern_trades
        result.append(item)
    result.sort(
        key=lambda item: (
            not bool(item["sample_sufficient"]),
            -float(item["average_net_pnl_usdt"]),
            -int(item["trade_count"]),
            str(item["pattern"]),
        )
    )
    return result


def _summary(records: Sequence[CryptoTradeDerivativesContext]) -> dict[str, Any]:
    if not records:
        return {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": None,
            "total_net_pnl_usdt": 0.0,
            "average_net_pnl_usdt": 0.0,
            "profit_factor": None,
            "average_mfe_r": None,
            "average_mae_r": None,
        }
    wins = [item for item in records if item.net_pnl_usdt > 0]
    losses = [item for item in records if item.net_pnl_usdt < 0]
    total = sum((item.net_pnl_usdt for item in records), start=_ZERO)
    gross_profit = sum((item.net_pnl_usdt for item in wins), start=_ZERO)
    gross_loss = -sum((item.net_pnl_usdt for item in losses), start=_ZERO)
    return {
        "trade_count": len(records),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": float(Decimal(len(wins)) / Decimal(len(records))),
        "total_net_pnl_usdt": float(total),
        "average_net_pnl_usdt": float(total / Decimal(len(records))),
        "profit_factor": (
            None if gross_loss == 0 else float(gross_profit / gross_loss)
        ),
        "average_mfe_r": float(
            sum((item.maximum_favorable_r for item in records), start=_ZERO)
            / Decimal(len(records))
        ),
        "average_mae_r": float(
            sum((item.maximum_adverse_r for item in records), start=_ZERO)
            / Decimal(len(records))
        ),
    }


def _validate_research_replay_boundary(replay: Mapping[str, Any]) -> None:
    for field in ("strategy_promotion_allowed", "bybit_live_order_routing_allowed"):
        if replay.get(field) is not False:
            raise ValueError(
                f"derivatives context rejected replay without explicit {field}=false"
            )


def _milliseconds(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("derivatives context timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("derivatives context timestamp must be timezone-aware")
    return int(parsed.timestamp() * 1000)


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"derivatives context missing {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"derivatives context missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"derivatives context invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"derivatives context non-finite {field}")
    return parsed
