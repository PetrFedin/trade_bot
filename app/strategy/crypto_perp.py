from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from app.marketdata.bybit_v5 import BybitKlineBar

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


class CryptoSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class CryptoPerpStrategyConfig:
    fast_ema_bars: int = 9
    slow_ema_bars: int = 21
    momentum_bars: int = 12
    breakout_bars: int = 20
    atr_bars: int = 14
    turnover_bars: int = 12
    minimum_average_turnover_usdt: Decimal = Decimal("500000")
    minimum_atr_fraction: Decimal = Decimal("0.0015")
    maximum_atr_fraction: Decimal = Decimal("0.025")
    minimum_abs_momentum: Decimal = Decimal("0.0025")
    minimum_quality_score: Decimal = Decimal("1.10")
    maximum_one_bar_atr_multiple: Decimal = Decimal("2.50")
    risk_fraction_per_trade: Decimal = Decimal("0.01")
    maximum_notional_to_equity: Decimal = Decimal("2.0")
    hard_stop_atr_multiple: Decimal = Decimal("1.0")
    expected_move_atr_multiple: Decimal = Decimal("3.0")
    target_net_profit_usd: Decimal = Decimal("15")
    taker_fee_rate: Decimal = Decimal("0.0006")
    slippage_bps_per_fill: Decimal = Decimal("2")
    maximum_concurrent_positions: int = 2

    def validate(self) -> None:
        positive_ints = (
            self.fast_ema_bars,
            self.slow_ema_bars,
            self.momentum_bars,
            self.breakout_bars,
            self.atr_bars,
            self.turnover_bars,
            self.maximum_concurrent_positions,
        )
        if any(value <= 0 for value in positive_ints):
            raise ValueError("crypto strategy bar counts and concurrency must be positive")
        if self.fast_ema_bars >= self.slow_ema_bars:
            raise ValueError("fast EMA must be shorter than slow EMA")
        non_negative = (
            self.minimum_average_turnover_usdt,
            self.minimum_atr_fraction,
            self.minimum_abs_momentum,
            self.minimum_quality_score,
            self.slippage_bps_per_fill,
        )
        if any(value < 0 for value in non_negative):
            raise ValueError("crypto strategy non-negative fields cannot be negative")
        positive = (
            self.maximum_atr_fraction,
            self.maximum_one_bar_atr_multiple,
            self.risk_fraction_per_trade,
            self.maximum_notional_to_equity,
            self.hard_stop_atr_multiple,
            self.expected_move_atr_multiple,
            self.target_net_profit_usd,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("crypto strategy positive fields must be positive")
        if self.minimum_atr_fraction >= self.maximum_atr_fraction:
            raise ValueError("minimum ATR fraction must be below maximum")
        if not _ZERO <= self.taker_fee_rate < _ONE:
            raise ValueError("taker fee rate must be in [0, 1)")
        if not _ZERO < self.risk_fraction_per_trade < _ONE:
            raise ValueError("risk_fraction_per_trade must be in (0, 1)")

    def with_target(self, target_usd: Decimal) -> CryptoPerpStrategyConfig:
        return replace(self, target_net_profit_usd=target_usd)


@dataclass(frozen=True)
class CryptoSignal:
    symbol: str
    side: CryptoSide
    reference_price: Decimal
    momentum: Decimal
    atr_fraction: Decimal
    fast_ema: Decimal
    slow_ema: Decimal
    breakout_strength_atr: Decimal
    one_bar_atr_multiple: Decimal
    average_turnover_usdt: Decimal
    quality_score: Decimal
    decision_time: str


@dataclass(frozen=True)
class CryptoSignalEvaluation:
    symbol: str
    eligible: bool
    reasons: tuple[str, ...]
    signal: CryptoSignal | None


@dataclass(frozen=True)
class CryptoTradePlan:
    symbol: str
    side: CryptoSide
    decision_time: str
    reference_price: Decimal
    notional_usdt: Decimal
    reference_quantity: Decimal
    risk_budget_usdt: Decimal
    stop_fraction: Decimal
    estimated_round_trip_cost_usdt: Decimal
    estimated_stop_loss_after_cost_usdt: Decimal
    target_net_profit_usd: Decimal
    required_move_fraction: Decimal
    expected_move_fraction: Decimal
    expected_net_edge_usd: Decimal
    quality_score: Decimal


@dataclass(frozen=True)
class CryptoTradePlanEvaluation:
    eligible: bool
    reasons: tuple[str, ...]
    plan: CryptoTradePlan | None


@dataclass(frozen=True)
class CryptoExecutionLevels:
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal


def minimum_history_bars(config: CryptoPerpStrategyConfig) -> int:
    config.validate()
    return max(
        config.slow_ema_bars + 2,
        config.momentum_bars + 2,
        config.breakout_bars + 2,
        config.atr_bars + 2,
        config.turnover_bars + 1,
    )


def evaluate_crypto_signal(
    bars: Sequence[BybitKlineBar],
    config: CryptoPerpStrategyConfig,
) -> CryptoSignalEvaluation:
    config.validate()
    if len(bars) < minimum_history_bars(config):
        symbol = bars[-1].symbol if bars else ""
        return CryptoSignalEvaluation(symbol, False, ("INSUFFICIENT_HISTORY",), None)
    symbol = bars[-1].symbol
    if any(bar.symbol != symbol for bar in bars):
        raise ValueError("crypto signal bars must belong to one symbol")
    ordered = sorted(bars, key=lambda bar: bar.start_time)
    if list(bars) != ordered:
        raise ValueError("crypto signal bars must be chronological")

    closes = [bar.close for bar in bars]
    current = bars[-1]
    previous = bars[-2]
    fast = _ema(closes, config.fast_ema_bars)
    slow = _ema(closes, config.slow_ema_bars)
    atr = _atr(bars, config.atr_bars)
    atr_fraction = atr / current.close
    momentum = current.close / closes[-1 - config.momentum_bars] - _ONE
    previous_high = max(bar.high for bar in bars[-1 - config.breakout_bars : -1])
    previous_low = min(bar.low for bar in bars[-1 - config.breakout_bars : -1])
    one_bar_atr = abs(current.close - previous.close) / atr if atr > 0 else Decimal("Infinity")
    average_turnover = _mean([bar.turnover for bar in bars[-config.turnover_bars :]])

    long_trend = current.close > fast > slow and momentum >= config.minimum_abs_momentum
    short_trend = current.close < fast < slow and momentum <= -config.minimum_abs_momentum
    if long_trend:
        side = CryptoSide.LONG
        breakout_strength = (current.close - previous_high) / atr
        trend_strength = (fast - slow) / atr
        directional_momentum = momentum / atr_fraction
    elif short_trend:
        side = CryptoSide.SHORT
        breakout_strength = (previous_low - current.close) / atr
        trend_strength = (slow - fast) / atr
        directional_momentum = (-momentum) / atr_fraction
    else:
        reasons: list[str] = ["TREND_MOMENTUM_NOT_ALIGNED"]
        reasons.extend(
            _market_quality_reasons(atr_fraction, average_turnover, one_bar_atr, config)
        )
        return CryptoSignalEvaluation(symbol, False, tuple(dict.fromkeys(reasons)), None)

    quality_score = directional_momentum + trend_strength + breakout_strength
    reasons = _market_quality_reasons(atr_fraction, average_turnover, one_bar_atr, config)
    if quality_score < config.minimum_quality_score:
        reasons.append("QUALITY_SCORE_BELOW_MINIMUM")
    if breakout_strength < Decimal("-0.50"):
        reasons.append("TOO_FAR_FROM_DIRECTIONAL_BREAKOUT")
    if reasons:
        return CryptoSignalEvaluation(symbol, False, tuple(reasons), None)

    return CryptoSignalEvaluation(
        symbol=symbol,
        eligible=True,
        reasons=(),
        signal=CryptoSignal(
            symbol=symbol,
            side=side,
            reference_price=current.close,
            momentum=momentum,
            atr_fraction=atr_fraction,
            fast_ema=fast,
            slow_ema=slow,
            breakout_strength_atr=breakout_strength,
            one_bar_atr_multiple=one_bar_atr,
            average_turnover_usdt=average_turnover,
            quality_score=quality_score,
            decision_time=current.start_time.isoformat(),
        ),
    )


def rank_crypto_signals(
    bars_by_symbol: dict[str, Sequence[BybitKlineBar]],
    config: CryptoPerpStrategyConfig,
) -> tuple[CryptoSignalEvaluation, ...]:
    evaluations = [evaluate_crypto_signal(bars, config) for bars in bars_by_symbol.values()]
    return tuple(
        sorted(
            evaluations,
            key=lambda item: (
                item.signal is not None,
                item.signal.quality_score if item.signal is not None else Decimal("-Infinity"),
                item.symbol,
            ),
            reverse=True,
        )
    )


def build_trade_plan(
    signal: CryptoSignal,
    *,
    equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
) -> CryptoTradePlanEvaluation:
    config.validate()
    if equity_usdt <= 0:
        raise ValueError("equity_usdt must be positive")
    stop_fraction = signal.atr_fraction * config.hard_stop_atr_multiple
    if stop_fraction <= 0:
        return CryptoTradePlanEvaluation(False, ("INVALID_STOP_DISTANCE",), None)

    per_fill_cost_fraction = config.taker_fee_rate + config.slippage_bps_per_fill / _BPS
    round_trip_cost_fraction = per_fill_cost_fraction * Decimal("2")
    stop_loss_after_cost_fraction = stop_fraction + round_trip_cost_fraction
    risk_budget = equity_usdt * config.risk_fraction_per_trade
    risk_sized_notional = risk_budget / stop_loss_after_cost_fraction
    notional = min(risk_sized_notional, equity_usdt * config.maximum_notional_to_equity)
    if notional <= 0:
        return CryptoTradePlanEvaluation(False, ("NO_NOTIONAL_AVAILABLE",), None)

    round_trip_cost_usdt = notional * round_trip_cost_fraction
    stop_loss_after_cost_usdt = notional * stop_loss_after_cost_fraction
    required_move = config.target_net_profit_usd / notional + round_trip_cost_fraction
    expected_move = signal.atr_fraction * config.expected_move_atr_multiple
    expected_net_edge = notional * expected_move - round_trip_cost_usdt
    reasons = []
    if expected_move < required_move:
        reasons.append("TARGET_NET_EDGE_UNAVAILABLE")
    if expected_net_edge < config.target_net_profit_usd:
        reasons.append("EXPECTED_NET_PROFIT_BELOW_TARGET")
    if stop_loss_after_cost_usdt > risk_budget:
        reasons.append("RISK_BUDGET_EXCEEDED_AFTER_COST")
    if reasons:
        return CryptoTradePlanEvaluation(False, tuple(reasons), None)

    return CryptoTradePlanEvaluation(
        eligible=True,
        reasons=(),
        plan=CryptoTradePlan(
            symbol=signal.symbol,
            side=signal.side,
            decision_time=signal.decision_time,
            reference_price=signal.reference_price,
            notional_usdt=notional,
            reference_quantity=notional / signal.reference_price,
            risk_budget_usdt=risk_budget,
            stop_fraction=stop_fraction,
            estimated_round_trip_cost_usdt=round_trip_cost_usdt,
            estimated_stop_loss_after_cost_usdt=stop_loss_after_cost_usdt,
            target_net_profit_usd=config.target_net_profit_usd,
            required_move_fraction=required_move,
            expected_move_fraction=expected_move,
            expected_net_edge_usd=expected_net_edge,
            quality_score=signal.quality_score,
        ),
    )


def execution_levels(
    plan: CryptoTradePlan,
    *,
    entry_price: Decimal,
    config: CryptoPerpStrategyConfig,
) -> CryptoExecutionLevels:
    config.validate()
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    quantity = plan.notional_usdt / entry_price
    per_fill_cost_fraction = config.taker_fee_rate + config.slippage_bps_per_fill / _BPS
    estimated_entry_cost = plan.notional_usdt * per_fill_cost_fraction
    estimated_exit_cost = plan.notional_usdt * per_fill_cost_fraction
    gross_profit_needed = config.target_net_profit_usd + estimated_entry_cost + estimated_exit_cost
    target_move = gross_profit_needed / quantity
    stop_move = entry_price * plan.stop_fraction
    if plan.side is CryptoSide.LONG:
        return CryptoExecutionLevels(
            entry_price=entry_price,
            stop_price=entry_price - stop_move,
            target_price=entry_price + target_move,
        )
    return CryptoExecutionLevels(
        entry_price=entry_price,
        stop_price=entry_price + stop_move,
        target_price=entry_price - target_move,
    )


def _market_quality_reasons(
    atr_fraction: Decimal,
    average_turnover: Decimal,
    one_bar_atr: Decimal,
    config: CryptoPerpStrategyConfig,
) -> list[str]:
    reasons: list[str] = []
    if atr_fraction < config.minimum_atr_fraction:
        reasons.append("VOLATILITY_TOO_LOW")
    if atr_fraction > config.maximum_atr_fraction:
        reasons.append("VOLATILITY_TOO_HIGH")
    if average_turnover < config.minimum_average_turnover_usdt:
        reasons.append("LIQUIDITY_TOO_LOW")
    if one_bar_atr > config.maximum_one_bar_atr_multiple:
        reasons.append("ONE_BAR_CHASE_RISK")
    return reasons


def _ema(values: Sequence[Decimal], period: int) -> Decimal:
    if period <= 0 or len(values) < period:
        raise ValueError("invalid EMA period")
    seed = _mean(values[:period])
    alpha = Decimal("2") / Decimal(period + 1)
    result = seed
    for value in values[period:]:
        result = alpha * value + (_ONE - alpha) * result
    return result


def _atr(bars: Sequence[BybitKlineBar], period: int) -> Decimal:
    if period <= 0 or len(bars) < period + 1:
        raise ValueError("invalid ATR period")
    true_ranges: list[Decimal] = []
    for index in range(len(bars) - period, len(bars)):
        current = bars[index]
        previous_close = bars[index - 1].close
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
        )
    return _mean(true_ranges)


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean requires values")
    return sum(values, start=_ZERO) / Decimal(len(values))
