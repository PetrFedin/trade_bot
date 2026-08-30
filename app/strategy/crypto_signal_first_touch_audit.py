from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    CryptoTradePlan,
    build_trade_plan,
    evaluate_crypto_signal,
    minimum_history_bars,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")
_INTERVAL = timedelta(minutes=5)
_Z_95 = 1.959963984540054
_FIRST_TOUCH_STATES = frozenset(
    {"TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_BAR", "NEITHER", "INCOMPLETE"}
)


@dataclass(frozen=True)
class CryptoSignalFirstTouchPolicy:
    horizon_minutes: int = 240
    minimum_pattern_observations: int = 5
    sample_sufficient_observations: int = 30
    minimum_cross_symbol_count: int = 2

    def validate(self) -> None:
        if self.horizon_minutes < 5 or self.horizon_minutes % 5 != 0:
            raise ValueError("first-touch horizon must be a positive 5m multiple")
        if not 1 <= self.minimum_pattern_observations <= self.sample_sufficient_observations:
            raise ValueError("first-touch minimum observations are invalid")
        if self.sample_sufficient_observations > 100_000:
            raise ValueError("first-touch sufficient observation count is unreasonable")
        if not 1 <= self.minimum_cross_symbol_count <= 1000:
            raise ValueError("first-touch minimum cross-symbol count is invalid")


@dataclass(frozen=True)
class CryptoModeledEntryLevels:
    entry_execution_price: Decimal
    quantity: Decimal
    entry_fee_usdt: Decimal
    hard_stop_raw_price: Decimal
    target_raw_price: Decimal
    risk_price_distance: Decimal

    def validate(self, *, side: CryptoSide) -> None:
        values = (
            self.entry_execution_price,
            self.quantity,
            self.entry_fee_usdt,
            self.hard_stop_raw_price,
            self.target_raw_price,
            self.risk_price_distance,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("first-touch modeled entry levels must be finite")
        if min(
            self.entry_execution_price,
            self.quantity,
            self.hard_stop_raw_price,
            self.target_raw_price,
            self.risk_price_distance,
        ) <= 0:
            raise ValueError("first-touch modeled prices, quantity and risk must be positive")
        if self.entry_fee_usdt < 0:
            raise ValueError("first-touch modeled entry fee cannot be negative")
        if side is CryptoSide.LONG:
            if not self.hard_stop_raw_price < self.entry_execution_price < self.target_raw_price:
                raise ValueError("first-touch LONG modeled levels are inconsistent")
        elif not self.target_raw_price < self.entry_execution_price < self.hard_stop_raw_price:
            raise ValueError("first-touch SHORT modeled levels are inconsistent")


@dataclass(frozen=True)
class CryptoSignalFirstTouchOutcome:
    symbol: str
    side: str
    decision_time: str
    signal_available_at: str
    quality_score: Decimal
    quality_ratio_to_entry_gate: Decimal
    clarity_band: str
    momentum_to_atr: Decimal
    trend_strength_atr: Decimal
    breakout_strength_atr: Decimal
    atr_fraction: Decimal
    one_bar_atr_multiple: Decimal
    average_turnover_usdt: Decimal
    expected_net_edge_usd: Decimal
    first_touch_state: str
    first_touch_bar: str | None
    maximum_favorable_r: Decimal | None
    maximum_adverse_r: Decimal | None
    modeled_stop_net_pnl_usdt: Decimal
    pattern: str

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("first-touch outcome symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("first-touch outcome side is invalid")
        if self.first_touch_state not in _FIRST_TOUCH_STATES:
            raise ValueError("first-touch state is invalid")
        if self.first_touch_state in {"TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_BAR"}:
            if self.first_touch_bar is None:
                raise ValueError("ordered/ambiguous first touch requires a bar timestamp")
        elif self.first_touch_bar is not None:
            raise ValueError("untouched first-touch outcome cannot carry a bar timestamp")
        values = (
            self.quality_score,
            self.quality_ratio_to_entry_gate,
            self.momentum_to_atr,
            self.trend_strength_atr,
            self.breakout_strength_atr,
            self.atr_fraction,
            self.one_bar_atr_multiple,
            self.average_turnover_usdt,
            self.expected_net_edge_usd,
            self.modeled_stop_net_pnl_usdt,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("first-touch outcome metrics must be finite")
        if self.maximum_favorable_r is not None and self.maximum_favorable_r < 0:
            raise ValueError("first-touch MFE cannot be negative")
        if self.maximum_adverse_r is not None and self.maximum_adverse_r < 0:
            raise ValueError("first-touch MAE cannot be negative")
        if not self.pattern:
            raise ValueError("first-touch pattern is required")


def audit_crypto_plan_eligible_first_touch(
    acquisition: BybitKlineAcquisition,
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    reference_equity_usdt: Decimal = Decimal("1000"),
    policy: CryptoSignalFirstTouchPolicy | None = None,
) -> dict[str, Any]:
    """Audit target-first vs stop-first for every fixed-rule plan-eligible signal.

    The audit is retrospective and standardizes every signal to the same reference equity. It
    deliberately ignores portfolio slots/cooldowns and does not change or promote strategy rules.
    Entry, hard-stop and net-target levels use the same fee/slippage algebra as the canonical
    replay; ambiguous same-bar target/stop touches are retained as ambiguity, never counted as a
    target-first success.
    """

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    active = CryptoSignalFirstTouchPolicy() if policy is None else policy
    active.validate()
    if not reference_equity_usdt.is_finite() or reference_equity_usdt <= 0:
        raise ValueError("first-touch reference equity must be positive and finite")

    outcomes: list[CryptoSignalFirstTouchOutcome] = []
    for symbol, bars in sorted(_bars_by_symbol(acquisition.bars).items()):
        minimum = minimum_history_bars(config)
        for index in range(minimum - 1, len(bars) - 1):
            history = bars[: index + 1]
            evaluation = evaluate_crypto_signal(history, config)
            if not evaluation.eligible or evaluation.signal is None:
                continue
            signal = evaluation.signal
            plan_evaluation = build_trade_plan(
                signal,
                equity_usdt=reference_equity_usdt,
                config=config,
            )
            if not plan_evaluation.eligible or plan_evaluation.plan is None:
                continue
            next_bar = bars[index + 1]
            if next_bar.start_time != bars[index].start_time + _INTERVAL:
                continue
            levels = model_crypto_signal_entry_levels(
                signal,
                plan_evaluation.plan,
                raw_next_open_price=next_bar.open,
                config=config,
            )
            window = _contiguous_window(
                bars[index + 1 :],
                start=next_bar.start_time,
                minutes=active.horizon_minutes,
            )
            state, first_touch_bar = evaluate_crypto_signal_first_touch(
                side=signal.side,
                levels=levels,
                bars=window,
                complete=bool(window),
            )
            mfe_r, mae_r = _excursions_r(signal.side, levels, window)
            atr = signal.reference_price * signal.atr_fraction
            if atr <= 0:
                raise ValueError("first-touch signal ATR must be positive")
            trend_strength = abs(signal.fast_ema - signal.slow_ema) / atr
            momentum_to_atr = abs(signal.momentum) / signal.atr_fraction
            ratio = signal.quality_score / config.minimum_quality_score
            outcome = CryptoSignalFirstTouchOutcome(
                symbol=symbol,
                side=signal.side.value,
                decision_time=signal.decision_time,
                signal_available_at=next_bar.start_time.isoformat(),
                quality_score=signal.quality_score,
                quality_ratio_to_entry_gate=ratio,
                clarity_band=_clarity_band(ratio),
                momentum_to_atr=momentum_to_atr,
                trend_strength_atr=trend_strength,
                breakout_strength_atr=signal.breakout_strength_atr,
                atr_fraction=signal.atr_fraction,
                one_bar_atr_multiple=signal.one_bar_atr_multiple,
                average_turnover_usdt=signal.average_turnover_usdt,
                expected_net_edge_usd=plan_evaluation.plan.expected_net_edge_usd,
                first_touch_state=state,
                first_touch_bar=first_touch_bar,
                maximum_favorable_r=mfe_r,
                maximum_adverse_r=mae_r,
                modeled_stop_net_pnl_usdt=_modeled_net_pnl_at_raw_exit(
                    side=signal.side,
                    levels=levels,
                    raw_exit_price=levels.hard_stop_raw_price,
                    config=config,
                ),
                pattern=_pattern(signal, ratio, trend_strength, config),
            )
            outcome.validate()
            outcomes.append(outcome)

    by_symbol: dict[str, list[CryptoSignalFirstTouchOutcome]] = defaultdict(list)
    by_side: dict[str, list[CryptoSignalFirstTouchOutcome]] = defaultdict(list)
    by_clarity: dict[str, list[CryptoSignalFirstTouchOutcome]] = defaultdict(list)
    by_pattern: dict[str, list[CryptoSignalFirstTouchOutcome]] = defaultdict(list)
    for item in outcomes:
        by_symbol[item.symbol].append(item)
        by_side[item.side].append(item)
        by_clarity[item.clarity_band].append(item)
        by_pattern[item.pattern].append(item)

    pattern_rows = [
        _pattern_summary(pattern, rows, policy=active)
        for pattern, rows in sorted(by_pattern.items())
    ]
    qualified = [
        row
        for row in pattern_rows
        if row["minimum_support_met"] and row["cross_symbol_support_met"]
    ]
    perfect = [row for row in qualified if row["observed_perfect_target_first"]]
    return {
        "audit": "BYBIT_CRYPTO_PLAN_ELIGIBLE_FIRST_TOUCH_V1",
        "reference_equity_usdt": float(reference_equity_usdt),
        "horizon_minutes": active.horizon_minutes,
        "plan_eligible_signal_count": len(outcomes),
        "symbol_count": len(by_symbol),
        "symbols": sorted(by_symbol),
        "aggregate": _summary(outcomes),
        "by_symbol": {key: _summary(rows) for key, rows in sorted(by_symbol.items())},
        "by_side": {key: _summary(rows) for key, rows in sorted(by_side.items())},
        "by_clarity_band": {
            key: _summary(rows) for key, rows in sorted(by_clarity.items())
        },
        "pattern_rows": sorted(pattern_rows, key=_pattern_sort_key, reverse=True),
        "qualified_pattern_rows": sorted(qualified, key=_pattern_sort_key, reverse=True),
        "retrospective_perfect_target_first_patterns": sorted(
            perfect,
            key=_pattern_sort_key,
            reverse=True,
        ),
        "perfect_target_first_pattern_count": len(perfect),
        "minimum_pattern_observations": active.minimum_pattern_observations,
        "sample_sufficient_observations": active.sample_sufficient_observations,
        "minimum_cross_symbol_count": active.minimum_cross_symbol_count,
        "success_definition": (
            "TARGET_FIRST within the fixed horizon; ambiguous same-bar, stop-first, neither and "
            "incomplete observations are not successes"
        ),
        "pattern_definition": (
            "side|clarity|configured-volatility-third|trend>=1ATR|breakout-confirmed-vs-pullback"
        ),
        "pattern_thresholds_fitted_to_outcomes": False,
        "portfolio_slot_constraints_applied": False,
        "cooldown_constraints_applied": False,
        "retrospective_only": True,
        "counterfactual_portfolio_pnl_claim_allowed": False,
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def model_crypto_signal_entry_levels(
    signal: CryptoSignal,
    plan: CryptoTradePlan,
    *,
    raw_next_open_price: Decimal,
    config: CryptoPerpStrategyConfig,
) -> CryptoModeledEntryLevels:
    """Model canonical next-open execution levels without executing a trade."""

    config.validate()
    if signal.symbol != plan.symbol or signal.side is not plan.side:
        raise ValueError("first-touch signal/plan identity mismatch")
    if raw_next_open_price <= 0:
        raise ValueError("first-touch raw next open must be positive")
    slippage = config.slippage_bps_per_fill / _BPS
    if signal.side is CryptoSide.LONG:
        entry = raw_next_open_price * (_ONE + slippage)
    else:
        entry = raw_next_open_price * (_ONE - slippage)
    quantity = plan.reference_quantity
    entry_fee = entry * quantity * config.taker_fee_rate
    risk_distance = entry * plan.stop_fraction
    hard_stop = entry - risk_distance if signal.side is CryptoSide.LONG else entry + risk_distance
    target = _raw_trigger_for_net_pnl(
        side=signal.side,
        entry_price=entry,
        quantity=quantity,
        entry_fee=entry_fee,
        desired_net_pnl=plan.target_net_profit_usd,
        config=config,
    )
    levels = CryptoModeledEntryLevels(
        entry_execution_price=entry,
        quantity=quantity,
        entry_fee_usdt=entry_fee,
        hard_stop_raw_price=hard_stop,
        target_raw_price=target,
        risk_price_distance=risk_distance,
    )
    levels.validate(side=signal.side)
    return levels


def evaluate_crypto_signal_first_touch(
    *,
    side: CryptoSide,
    levels: CryptoModeledEntryLevels,
    bars: Sequence[BybitKlineBar],
    complete: bool,
) -> tuple[str, str | None]:
    levels.validate(side=side)
    if not complete:
        return "INCOMPLETE", None
    for bar in bars:
        if side is CryptoSide.LONG:
            target = bar.high >= levels.target_raw_price
            stop = bar.low <= levels.hard_stop_raw_price
        else:
            target = bar.low <= levels.target_raw_price
            stop = bar.high >= levels.hard_stop_raw_price
        if target and stop:
            return "AMBIGUOUS_SAME_BAR", bar.start_time.isoformat()
        if target:
            return "TARGET_FIRST", bar.start_time.isoformat()
        if stop:
            return "STOP_FIRST", bar.start_time.isoformat()
    return "NEITHER", None


def _raw_trigger_for_net_pnl(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    quantity: Decimal,
    entry_fee: Decimal,
    desired_net_pnl: Decimal,
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    if quantity <= 0:
        raise ValueError("first-touch quantity must be positive")
    fee = config.taker_fee_rate
    slippage = config.slippage_bps_per_fill / _BPS
    if side is CryptoSide.LONG:
        exit_execution = (desired_net_pnl + quantity * entry_price + entry_fee) / (
            quantity * (_ONE - fee)
        )
        return exit_execution / (_ONE - slippage)
    exit_execution = (quantity * entry_price - entry_fee - desired_net_pnl) / (
        quantity * (_ONE + fee)
    )
    if exit_execution <= 0:
        raise ValueError("first-touch short target would require non-positive exit price")
    return exit_execution / (_ONE + slippage)


def _modeled_net_pnl_at_raw_exit(
    *,
    side: CryptoSide,
    levels: CryptoModeledEntryLevels,
    raw_exit_price: Decimal,
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    slippage = config.slippage_bps_per_fill / _BPS
    if side is CryptoSide.LONG:
        exit_price = raw_exit_price * (_ONE - slippage)
        gross = (exit_price - levels.entry_execution_price) * levels.quantity
    else:
        exit_price = raw_exit_price * (_ONE + slippage)
        gross = (levels.entry_execution_price - exit_price) * levels.quantity
    exit_fee = exit_price * levels.quantity * config.taker_fee_rate
    return gross - levels.entry_fee_usdt - exit_fee


def _contiguous_window(
    bars: Sequence[BybitKlineBar],
    *,
    start: Any,
    minutes: int,
) -> tuple[BybitKlineBar, ...]:
    expected_count = minutes // 5
    expected = tuple(start + index * _INTERVAL for index in range(expected_count))
    by_start = {bar.start_time: bar for bar in bars if bar.start_time in expected}
    if len(by_start) != expected_count:
        return ()
    return tuple(by_start[timestamp] for timestamp in expected)


def _excursions_r(
    side: CryptoSide,
    levels: CryptoModeledEntryLevels,
    bars: Sequence[BybitKlineBar],
) -> tuple[Decimal | None, Decimal | None]:
    if not bars:
        return None, None
    if side is CryptoSide.LONG:
        favorable = max(bar.high for bar in bars) - levels.entry_execution_price
        adverse = levels.entry_execution_price - min(bar.low for bar in bars)
    else:
        favorable = levels.entry_execution_price - min(bar.low for bar in bars)
        adverse = max(bar.high for bar in bars) - levels.entry_execution_price
    return (
        max(favorable, _ZERO) / levels.risk_price_distance,
        max(adverse, _ZERO) / levels.risk_price_distance,
    )


def _pattern(
    signal: CryptoSignal,
    quality_ratio: Decimal,
    trend_strength: Decimal,
    config: CryptoPerpStrategyConfig,
) -> str:
    span = config.maximum_atr_fraction - config.minimum_atr_fraction
    lower = config.minimum_atr_fraction + span / Decimal("3")
    upper = config.minimum_atr_fraction + span * Decimal("2") / Decimal("3")
    if signal.atr_fraction <= lower:
        volatility = "VOL_LOW_NORMAL"
    elif signal.atr_fraction <= upper:
        volatility = "VOL_MID_NORMAL"
    else:
        volatility = "VOL_HIGH_NORMAL"
    trend = "TREND_STRONG" if trend_strength >= _ONE else "TREND_MODERATE"
    breakout = "BREAKOUT_CONFIRMED" if signal.breakout_strength_atr >= _ZERO else "BREAKOUT_PULLBACK"
    return "|".join(
        (signal.side.value, _clarity_band(quality_ratio), volatility, trend, breakout)
    )


def _clarity_band(ratio: Decimal) -> str:
    if ratio < _ONE:
        raise ValueError("plan-eligible signal cannot be below quality gate")
    if ratio < Decimal("1.25"):
        return "MARGINAL"
    if ratio < Decimal("1.75"):
        return "CLEAR"
    return "STRONG"


def _bars_by_symbol(
    bars: Sequence[BybitKlineBar],
) -> dict[str, tuple[BybitKlineBar, ...]]:
    grouped: dict[str, list[BybitKlineBar]] = defaultdict(list)
    for bar in bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    result: dict[str, tuple[BybitKlineBar, ...]] = {}
    for symbol, rows in grouped.items():
        ordered = tuple(sorted(rows, key=lambda item: item.start_time))
        if len({bar.start_time for bar in ordered}) != len(ordered):
            raise ValueError("first-touch bars contain duplicate timestamps")
        result[symbol] = ordered
    return result


def _summary(rows: Sequence[CryptoSignalFirstTouchOutcome]) -> dict[str, Any]:
    counts = Counter(item.first_touch_state for item in rows)
    complete = [item for item in rows if item.first_touch_state != "INCOMPLETE"]
    mfe = [item.maximum_favorable_r for item in rows if item.maximum_favorable_r is not None]
    mae = [item.maximum_adverse_r for item in rows if item.maximum_adverse_r is not None]
    target_count = counts["TARGET_FIRST"]
    return {
        "observation_count": len(rows),
        "complete_count": len(complete),
        "target_first_count": target_count,
        "stop_first_count": counts["STOP_FIRST"],
        "ambiguous_same_bar_count": counts["AMBIGUOUS_SAME_BAR"],
        "neither_count": counts["NEITHER"],
        "incomplete_count": counts["INCOMPLETE"],
        "target_first_rate": None if not rows else target_count / len(rows),
        "target_first_rate_of_complete": (
            None if not complete else target_count / len(complete)
        ),
        "target_first_wilson_lower_95": _wilson_lower(target_count, len(rows)),
        "median_quality_ratio": (
            None
            if not rows
            else float(statistics.median(item.quality_ratio_to_entry_gate for item in rows))
        ),
        "median_expected_net_edge_usd": (
            None
            if not rows
            else float(statistics.median(item.expected_net_edge_usd for item in rows))
        ),
        "median_mfe_r": None if not mfe else float(statistics.median(mfe)),
        "median_mae_r": None if not mae else float(statistics.median(mae)),
        "median_modeled_stop_net_pnl_usdt": (
            None
            if not rows
            else float(statistics.median(item.modeled_stop_net_pnl_usdt for item in rows))
        ),
        "first_touch_state_counts": dict(sorted(counts.items())),
    }


def _pattern_summary(
    pattern: str,
    rows: Sequence[CryptoSignalFirstTouchOutcome],
    *,
    policy: CryptoSignalFirstTouchPolicy,
) -> dict[str, Any]:
    summary = _summary(rows)
    symbols = sorted({item.symbol for item in rows})
    minimum_support = len(rows) >= policy.minimum_pattern_observations
    cross_symbol_support = len(symbols) >= policy.minimum_cross_symbol_count
    perfect = bool(rows) and summary["target_first_count"] == len(rows)
    if perfect and minimum_support and cross_symbol_support:
        tier = (
            "RETROSPECTIVE_PERFECT_TARGET_FIRST_SAMPLE_SUFFICIENT"
            if len(rows) >= policy.sample_sufficient_observations
            else "RETROSPECTIVE_PERFECT_TARGET_FIRST_SMALL_SAMPLE"
        )
    elif minimum_support and cross_symbol_support:
        tier = "RETROSPECTIVE_MIXED"
    else:
        tier = "INSUFFICIENT_SUPPORT"
    return {
        "pattern": pattern,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "minimum_support_met": minimum_support,
        "cross_symbol_support_met": cross_symbol_support,
        "sample_sufficient": len(rows) >= policy.sample_sufficient_observations,
        "observed_perfect_target_first": perfect,
        "candidate_tier": tier,
        **summary,
    }


def _pattern_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        bool(row["observed_perfect_target_first"]),
        bool(row["sample_sufficient"]),
        float(row["target_first_rate"] or 0.0),
        int(row["observation_count"]),
        float(row["target_first_wilson_lower_95"] or 0.0),
        str(row["pattern"]),
    )


def _wilson_lower(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = _Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


__all__ = [
    "CryptoModeledEntryLevels",
    "CryptoSignalFirstTouchOutcome",
    "CryptoSignalFirstTouchPolicy",
    "audit_crypto_plan_eligible_first_touch",
    "evaluate_crypto_signal_first_touch",
    "model_crypto_signal_entry_levels",
]
