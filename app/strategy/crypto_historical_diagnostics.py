from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    evaluate_crypto_signal,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class CryptoHistoricalDiagnosticsPolicy:
    minimum_pattern_trades: int = 5
    quantile_buckets: int = 4

    def validate(self) -> None:
        if not 1 <= self.minimum_pattern_trades <= 10_000:
            raise ValueError("crypto diagnostics minimum pattern trades must be within [1, 10000]")
        if not 2 <= self.quantile_buckets <= 10:
            raise ValueError("crypto diagnostics quantile buckets must be within [2, 10]")


@dataclass(frozen=True)
class CryptoHistoricalTradeCondition:
    symbol: str
    side: str
    decision_time: str
    entry_time: str
    exit_time: str
    exit_reason: str
    net_pnl_usdt: Decimal
    maximum_favorable_r: Decimal
    maximum_adverse_r: Decimal
    holding_bars: int
    quality_score: Decimal
    momentum_abs: Decimal
    momentum_to_atr: Decimal
    atr_fraction: Decimal
    trend_strength_atr: Decimal
    breakout_strength_atr: Decimal
    one_bar_atr_multiple: Decimal
    average_turnover_usdt: Decimal
    expected_net_edge_usd: Decimal
    exit_mode: str
    volatility_regime: str
    trend_regime: str
    breakout_regime: str
    turnover_regime: str

    @property
    def repeated_pattern(self) -> str:
        return "|".join(
            (
                self.side,
                self.volatility_regime,
                self.trend_regime,
                self.breakout_regime,
                self.turnover_regime,
            )
        )


def build_crypto_historical_trade_conditions(
    acquisition: BybitKlineAcquisition,
    replay: Mapping[str, Any],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> tuple[CryptoHistoricalTradeCondition, ...]:
    """Reconstruct one point-in-time condition row for every closed replay trade."""

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    _validate_research_replay_boundary(replay)

    closed_trades = replay.get("closed_trades")
    decision_events = replay.get("decision_events")
    if not isinstance(closed_trades, list) or not isinstance(decision_events, list):
        raise ValueError("crypto diagnostics requires replay closed_trades and decision_events")

    bars_by_symbol = _bars_by_symbol(acquisition.bars)
    entry_events = _entry_event_map(decision_events)
    raw_records: list[dict[str, Any]] = []
    for raw_trade in closed_trades:
        if not isinstance(raw_trade, Mapping):
            raise ValueError("crypto diagnostics closed trade must be an object")
        symbol = _required_text(raw_trade, "symbol")
        side = _required_text(raw_trade, "side")
        decision_time = _required_text(raw_trade, "decision_time")
        entry_time = _required_text(raw_trade, "entry_time")
        exit_time = _required_text(raw_trade, "exit_time")
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            raise ValueError(f"crypto diagnostics trade symbol missing from acquisition:{symbol}")
        decision_at = _parse_time(decision_time)
        history = tuple(bar for bar in bars if bar.start_time <= decision_at)
        evaluation = evaluate_crypto_signal(history, config)
        if evaluation.signal is None or not evaluation.eligible:
            raise ValueError(
                f"crypto diagnostics cannot reconstruct accepted signal:{symbol}:{decision_time}"
            )
        signal = evaluation.signal
        if signal.side.value != side:
            raise ValueError("crypto diagnostics reconstructed side differs from replay trade")
        atr = signal.reference_price * signal.atr_fraction
        if atr <= 0:
            raise ValueError("crypto diagnostics reconstructed ATR must be positive")
        trend_strength = abs(signal.fast_ema - signal.slow_ema) / atr
        momentum_to_atr = abs(signal.momentum) / signal.atr_fraction
        entry_event = entry_events.get((symbol, entry_time))
        if entry_event is None:
            raise ValueError("crypto diagnostics cannot match closed trade to ENTRY event")
        expected_edge = _required_decimal(entry_event, "expected_net_edge_usd")
        raw_records.append(
            {
                "symbol": symbol,
                "side": side,
                "decision_time": decision_time,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "exit_reason": _required_text(raw_trade, "exit_reason"),
                "net_pnl_usdt": _required_decimal(raw_trade, "net_pnl_usdt"),
                "maximum_favorable_r": _required_decimal(
                    raw_trade, "maximum_favorable_r_before_exit"
                ),
                "maximum_adverse_r": _required_decimal(
                    raw_trade, "maximum_adverse_r_before_exit"
                ),
                "holding_bars": _required_non_negative_int(raw_trade, "holding_bars"),
                "quality_score": signal.quality_score,
                "momentum_abs": abs(signal.momentum),
                "momentum_to_atr": momentum_to_atr,
                "atr_fraction": signal.atr_fraction,
                "trend_strength_atr": trend_strength,
                "breakout_strength_atr": signal.breakout_strength_atr,
                "one_bar_atr_multiple": signal.one_bar_atr_multiple,
                "average_turnover_usdt": signal.average_turnover_usdt,
                "expected_net_edge_usd": expected_edge,
                "exit_mode": _required_text(entry_event, "exit_mode"),
            }
        )

    turnover_values = [record["average_turnover_usdt"] for record in raw_records]
    turnover_median = _median(turnover_values) if turnover_values else None
    return tuple(
        _materialize_record(
            record,
            config=config,
            turnover_median=turnover_median,
        )
        for record in raw_records
    )


def diagnose_crypto_historical_conditions(
    acquisition: BybitKlineAcquisition,
    replay: Mapping[str, Any],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    policy: CryptoHistoricalDiagnosticsPolicy | None = None,
) -> dict[str, Any]:
    """Explain conditional historical performance without changing or promoting the strategy.

    Features are reconstructed strictly from completed bars at each trade decision timestamp. The
    result describes observed associations only; it is not a causal claim and cannot authorize
    demo or live execution.
    """

    active = CryptoHistoricalDiagnosticsPolicy() if policy is None else policy
    active.validate()
    records = build_crypto_historical_trade_conditions(
        acquisition,
        replay,
        strategy_config=strategy_config,
    )
    bars_by_symbol = _bars_by_symbol(acquisition.bars)
    feature_quantiles = {
        feature: _quantile_condition_table(
            records,
            feature=feature,
            buckets=active.quantile_buckets,
        )
        for feature in (
            "quality_score",
            "momentum_to_atr",
            "atr_fraction",
            "trend_strength_atr",
            "breakout_strength_atr",
            "one_bar_atr_multiple",
            "average_turnover_usdt",
            "expected_net_edge_usd",
        )
    }
    by_symbol = _group_table(records, key=lambda item: item.symbol)
    by_side = _group_table(records, key=lambda item: item.side)
    by_exit_mode = _group_table(records, key=lambda item: item.exit_mode)
    by_regime = _group_table(
        records,
        key=lambda item: "|".join(
            (
                item.volatility_regime,
                item.trend_regime,
                item.breakout_regime,
                item.turnover_regime,
            )
        ),
    )
    repeated_patterns = _repeated_patterns(records, policy=active)
    coverage = _coverage_by_symbol(bars_by_symbol)
    aggregate = _summary(records)

    return {
        "diagnostic": "BYBIT_CRYPTO_HISTORICAL_CONDITION_ASSOCIATIONS",
        "trade_count": len(records),
        "aggregate": aggregate,
        "coverage_by_symbol": coverage,
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_exit_mode": by_exit_mode,
        "by_market_regime": by_regime,
        "feature_quantiles": feature_quantiles,
        "repeated_patterns": repeated_patterns,
        "pattern_minimum_trade_count": active.minimum_pattern_trades,
        "quantile_buckets": active.quantile_buckets,
        "feature_timing_contract": (
            "completed bars at trade decision timestamp only; entry executes later at next open"
        ),
        "interpretation_contract": (
            "conditional historical associations; no feature or combination is treated as a "
            "causal explanation or a guarantee of future profitability"
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


def _materialize_record(
    raw: Mapping[str, Any],
    *,
    config: CryptoPerpStrategyConfig,
    turnover_median: Decimal | None,
) -> CryptoHistoricalTradeCondition:
    atr_fraction = raw["atr_fraction"]
    if not isinstance(atr_fraction, Decimal):
        raise ValueError("crypto diagnostics ATR fraction type is invalid")
    volatility_span = config.maximum_atr_fraction - config.minimum_atr_fraction
    lower = config.minimum_atr_fraction + volatility_span / Decimal("3")
    upper = config.minimum_atr_fraction + volatility_span * Decimal("2") / Decimal("3")
    if atr_fraction <= lower:
        volatility = "VOL_LOW_NORMAL"
    elif atr_fraction <= upper:
        volatility = "VOL_MID_NORMAL"
    else:
        volatility = "VOL_HIGH_NORMAL"

    trend_strength = raw["trend_strength_atr"]
    if not isinstance(trend_strength, Decimal):
        raise ValueError("crypto diagnostics trend strength type is invalid")
    trend = "TREND_STRONG" if trend_strength >= _ONE else "TREND_MODERATE"

    breakout_strength = raw["breakout_strength_atr"]
    if not isinstance(breakout_strength, Decimal):
        raise ValueError("crypto diagnostics breakout strength type is invalid")
    breakout = (
        "BREAKOUT_CONFIRMED"
        if breakout_strength >= _ZERO
        else "BREAKOUT_PULLBACK"
    )

    turnover = raw["average_turnover_usdt"]
    if not isinstance(turnover, Decimal):
        raise ValueError("crypto diagnostics turnover type is invalid")
    if turnover_median is None:
        turnover_regime = "TURNOVER_UNKNOWN"
    else:
        turnover_regime = (
            "TURNOVER_HIGH" if turnover >= turnover_median else "TURNOVER_LOW"
        )

    return CryptoHistoricalTradeCondition(
        symbol=str(raw["symbol"]),
        side=str(raw["side"]),
        decision_time=str(raw["decision_time"]),
        entry_time=str(raw["entry_time"]),
        exit_time=str(raw["exit_time"]),
        exit_reason=str(raw["exit_reason"]),
        net_pnl_usdt=raw["net_pnl_usdt"],
        maximum_favorable_r=raw["maximum_favorable_r"],
        maximum_adverse_r=raw["maximum_adverse_r"],
        holding_bars=int(raw["holding_bars"]),
        quality_score=raw["quality_score"],
        momentum_abs=raw["momentum_abs"],
        momentum_to_atr=raw["momentum_to_atr"],
        atr_fraction=atr_fraction,
        trend_strength_atr=trend_strength,
        breakout_strength_atr=breakout_strength,
        one_bar_atr_multiple=raw["one_bar_atr_multiple"],
        average_turnover_usdt=turnover,
        expected_net_edge_usd=raw["expected_net_edge_usd"],
        exit_mode=str(raw["exit_mode"]),
        volatility_regime=volatility,
        trend_regime=trend,
        breakout_regime=breakout,
        turnover_regime=turnover_regime,
    )


def _entry_event_map(events: Sequence[Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("event") != "ENTRY":
            continue
        symbol = _required_text(event, "symbol")
        entry_time = _required_text(event, "execution_time")
        key = (symbol, entry_time)
        if key in result:
            raise ValueError("crypto diagnostics replay has duplicate ENTRY event key")
        result[key] = event
    return result


def _bars_by_symbol(
    bars: Sequence[BybitKlineBar],
) -> dict[str, tuple[BybitKlineBar, ...]]:
    grouped: dict[str, list[BybitKlineBar]] = defaultdict(list)
    for bar in bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    result: dict[str, tuple[BybitKlineBar, ...]] = {}
    for symbol, values in grouped.items():
        ordered = tuple(sorted(values, key=lambda item: item.start_time))
        if len({bar.start_time for bar in ordered}) != len(ordered):
            raise ValueError("crypto diagnostics acquisition has duplicate symbol/timestamp")
        result[symbol] = ordered
    return result


def _coverage_by_symbol(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol, bars in sorted(bars_by_symbol.items()):
        if not bars:
            continue
        result[symbol] = {
            "bar_count": len(bars),
            "first_bar": bars[0].start_time.isoformat(),
            "last_bar": bars[-1].start_time.isoformat(),
        }
    return result


def _quantile_condition_table(
    records: Sequence[CryptoHistoricalTradeCondition],
    *,
    feature: str,
    buckets: int,
) -> list[dict[str, Any]]:
    if not records:
        return []
    values = [getattr(record, feature) for record in records]
    if any(
        not isinstance(value, Decimal) or not value.is_finite()
        for value in values
    ):
        raise ValueError(f"crypto diagnostics feature {feature} must be finite Decimal")
    ordered = sorted(values)
    grouped: dict[int, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    for record in records:
        value = getattr(record, feature)
        bucket = _quantile_bucket(value, ordered=ordered, buckets=buckets)
        grouped[bucket].append(record)
    table: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        members = grouped[bucket]
        member_values = [getattr(item, feature) for item in members]
        summary = _summary(members)
        table.append(
            {
                "bucket": bucket,
                "feature_min": float(min(member_values)),
                "feature_max": float(max(member_values)),
                **summary,
            }
        )
    return table


def _quantile_bucket(
    value: Decimal,
    *,
    ordered: Sequence[Decimal],
    buckets: int,
) -> int:
    less = sum(item < value for item in ordered)
    equal = sum(item == value for item in ordered)
    center_rank = Decimal(less) + Decimal(equal) / Decimal("2")
    fraction = center_rank / Decimal(len(ordered))
    bucket = int(fraction * Decimal(buckets)) + 1
    return min(buckets, max(1, bucket))


def _group_table(
    records: Sequence[CryptoHistoricalTradeCondition],
    *,
    key: Any,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    for record in records:
        grouped[str(key(record))].append(record)
    return {
        group: _summary(members)
        for group, members in sorted(grouped.items())
    }


def _repeated_patterns(
    records: Sequence[CryptoHistoricalTradeCondition],
    *,
    policy: CryptoHistoricalDiagnosticsPolicy,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    for record in records:
        grouped[record.repeated_pattern].append(record)
    payload: list[dict[str, Any]] = []
    for pattern, members in grouped.items():
        summary = _summary(members)
        summary["pattern"] = pattern
        summary["sample_sufficient"] = len(members) >= policy.minimum_pattern_trades
        payload.append(summary)
    payload.sort(
        key=lambda item: (
            not bool(item["sample_sufficient"]),
            -float(item["average_net_pnl_usdt"]),
            -int(item["trade_count"]),
            str(item["pattern"]),
        )
    )
    return payload


def _summary(records: Sequence[CryptoHistoricalTradeCondition]) -> dict[str, Any]:
    count = len(records)
    if count == 0:
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
            "average_holding_bars": None,
        }
    wins = [record for record in records if record.net_pnl_usdt > 0]
    losses = [record for record in records if record.net_pnl_usdt < 0]
    gross_profit = sum((record.net_pnl_usdt for record in wins), start=_ZERO)
    gross_loss = -sum((record.net_pnl_usdt for record in losses), start=_ZERO)
    total = sum((record.net_pnl_usdt for record in records), start=_ZERO)
    profit_factor = None if gross_loss == 0 else gross_profit / gross_loss
    return {
        "trade_count": count,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": float(Decimal(len(wins)) / Decimal(count)),
        "total_net_pnl_usdt": float(total),
        "average_net_pnl_usdt": float(total / Decimal(count)),
        "profit_factor": None if profit_factor is None else float(profit_factor),
        "average_mfe_r": float(
            sum((record.maximum_favorable_r for record in records), start=_ZERO)
            / Decimal(count)
        ),
        "average_mae_r": float(
            sum((record.maximum_adverse_r for record in records), start=_ZERO)
            / Decimal(count)
        ),
        "average_holding_bars": float(
            Decimal(sum(record.holding_bars for record in records)) / Decimal(count)
        ),
    }


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("crypto diagnostics median requires values")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _validate_research_replay_boundary(replay: Mapping[str, Any]) -> None:
    for field in (
        "strategy_promotion_allowed",
        "bybit_live_order_routing_allowed",
    ):
        if replay.get(field) is not False:
            raise ValueError(
                f"crypto diagnostics rejected replay without explicit {field}=false"
            )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("crypto diagnostics timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("crypto diagnostics timestamp must be timezone-aware")
    return parsed


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"crypto diagnostics missing {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"crypto diagnostics missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"crypto diagnostics invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"crypto diagnostics non-finite {field}")
    return parsed


def _required_non_negative_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"crypto diagnostics missing {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"crypto diagnostics invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"crypto diagnostics negative {field}")
    return parsed
