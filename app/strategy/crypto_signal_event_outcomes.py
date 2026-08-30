from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    evaluate_crypto_signal,
    minimum_history_bars,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_INTERVAL = timedelta(minutes=5)
_HORIZONS = (15, 60, 240)


@dataclass(frozen=True)
class CryptoSignalForwardHorizon:
    minutes: int
    complete: bool
    directional_return_fraction: Decimal | None


@dataclass(frozen=True)
class CryptoSignalEventOutcome:
    symbol: str
    side: str
    decision_time: str
    signal_available_at: str
    next_bar_open_time: str
    entry_reference_price: Decimal
    quality_score: Decimal
    quality_ratio_to_entry_gate: Decimal
    clarity_band: str
    momentum_to_atr: Decimal
    trend_strength_atr: Decimal
    breakout_strength_atr: Decimal
    atr_fraction: Decimal
    one_bar_atr_multiple: Decimal
    average_turnover_usdt: Decimal
    plan_eligible_at_reference_equity: bool
    plan_block_reasons: tuple[str, ...]
    maximum_favorable_r_240m: Decimal | None
    maximum_adverse_r_240m: Decimal | None
    horizons: tuple[CryptoSignalForwardHorizon, ...]


def audit_all_crypto_signal_events(
    acquisition: BybitKlineAcquisition,
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    reference_equity_usdt: Decimal = Decimal("1000"),
) -> dict[str, Any]:
    """Measure every eligible completed-bar signal independent of portfolio slot decisions."""

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    if not reference_equity_usdt.is_finite() or reference_equity_usdt <= 0:
        raise ValueError("signal event audit reference equity must be positive and finite")

    bars_by_symbol = _bars_by_symbol(acquisition.bars)
    outcomes: list[CryptoSignalEventOutcome] = []
    for symbol, bars in sorted(bars_by_symbol.items()):
        minimum = minimum_history_bars(config)
        for index in range(minimum - 1, len(bars) - 1):
            history = bars[: index + 1]
            evaluation = evaluate_crypto_signal(history, config)
            if not evaluation.eligible or evaluation.signal is None:
                continue
            signal = evaluation.signal
            next_bar = bars[index + 1]
            if next_bar.start_time != bars[index].start_time + _INTERVAL:
                continue
            atr = signal.reference_price * signal.atr_fraction
            if atr <= 0:
                raise ValueError("signal event audit ATR must be positive")
            trend_strength = abs(signal.fast_ema - signal.slow_ema) / atr
            momentum_to_atr = abs(signal.momentum) / signal.atr_fraction
            plan = build_trade_plan(
                signal,
                equity_usdt=reference_equity_usdt,
                config=config,
            )
            future = tuple(
                bar
                for bar in bars[index + 1 :]
                if bar.start_time < next_bar.start_time + timedelta(minutes=240)
            )
            full_240 = _contiguous_window(
                available_at=next_bar.start_time,
                bars=future,
                minutes=240,
            )
            mfe_r, mae_r = _excursions_r(
                side=signal.side.value,
                entry_price=next_bar.open,
                risk_distance=atr * config.hard_stop_atr_multiple,
                bars=full_240,
            )
            horizons = tuple(
                _horizon(
                    side=signal.side.value,
                    entry_price=next_bar.open,
                    available_at=next_bar.start_time,
                    bars=future,
                    minutes=minutes,
                )
                for minutes in _HORIZONS
            )
            ratio = signal.quality_score / config.minimum_quality_score
            outcomes.append(
                CryptoSignalEventOutcome(
                    symbol=symbol,
                    side=signal.side.value,
                    decision_time=signal.decision_time,
                    signal_available_at=(bars[index].start_time + _INTERVAL).isoformat(),
                    next_bar_open_time=next_bar.start_time.isoformat(),
                    entry_reference_price=next_bar.open,
                    quality_score=signal.quality_score,
                    quality_ratio_to_entry_gate=ratio,
                    clarity_band=_clarity_band(ratio),
                    momentum_to_atr=momentum_to_atr,
                    trend_strength_atr=trend_strength,
                    breakout_strength_atr=signal.breakout_strength_atr,
                    atr_fraction=signal.atr_fraction,
                    one_bar_atr_multiple=signal.one_bar_atr_multiple,
                    average_turnover_usdt=signal.average_turnover_usdt,
                    plan_eligible_at_reference_equity=plan.eligible,
                    plan_block_reasons=plan.reasons,
                    maximum_favorable_r_240m=mfe_r,
                    maximum_adverse_r_240m=mae_r,
                    horizons=horizons,
                )
            )

    by_symbol: dict[str, list[CryptoSignalEventOutcome]] = defaultdict(list)
    by_side: dict[str, list[CryptoSignalEventOutcome]] = defaultdict(list)
    by_clarity: dict[str, list[CryptoSignalEventOutcome]] = defaultdict(list)
    for item in outcomes:
        by_symbol[item.symbol].append(item)
        by_side[item.side].append(item)
        by_clarity[item.clarity_band].append(item)

    return {
        "audit": "BYBIT_CRYPTO_ALL_ELIGIBLE_SIGNAL_EVENTS_V1",
        "signal_event_count": len(outcomes),
        "symbol_count": len(by_symbol),
        "symbols": sorted(by_symbol),
        "reference_equity_usdt": float(reference_equity_usdt),
        "aggregate": _summary(outcomes),
        "by_symbol": {
            key: _summary(rows) for key, rows in sorted(by_symbol.items())
        },
        "by_side": {key: _summary(rows) for key, rows in sorted(by_side.items())},
        "by_clarity_band": {
            key: _summary(rows) for key, rows in sorted(by_clarity.items())
        },
        "signal_rows": [_to_payload(item) for item in outcomes],
        "clarity_contract": {
            "metric": "quality_score / frozen minimum_quality_score",
            "MARGINAL": "[1.00, 1.25)",
            "CLEAR": "[1.25, 1.75)",
            "STRONG": "[1.75, infinity)",
            "descriptive_only": True,
        },
        "movement_contract": (
            "signal becomes knowable after the completed decision bar; movement starts from "
            "the next contiguous 5m bar open and does not model an executed trade"
        ),
        "excursion_contract": (
            "240m MFE/MAE is emitted only when all 48 future 5m bars are contiguous"
        ),
        "portfolio_slot_constraints_applied": False,
        "cooldown_constraints_applied": False,
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "predictive_guarantee_allowed": False,
    }


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
            raise ValueError("signal event audit bars contain duplicate timestamp")
        result[symbol] = ordered
    return result


def _contiguous_window(
    *,
    available_at: datetime,
    bars: Sequence[BybitKlineBar],
    minutes: int,
) -> tuple[BybitKlineBar, ...]:
    expected_count = minutes // 5
    expected = tuple(available_at + index * _INTERVAL for index in range(expected_count))
    by_start = {bar.start_time: bar for bar in bars if bar.start_time in expected}
    if len(by_start) != expected_count:
        return ()
    return tuple(by_start[start] for start in expected)


def _horizon(
    *,
    side: str,
    entry_price: Decimal,
    available_at: datetime,
    bars: Sequence[BybitKlineBar],
    minutes: int,
) -> CryptoSignalForwardHorizon:
    window = _contiguous_window(
        available_at=available_at,
        bars=bars,
        minutes=minutes,
    )
    if not window:
        return CryptoSignalForwardHorizon(minutes, False, None)
    close_price = window[-1].close
    raw = close_price / entry_price - _ONE
    directional = raw if side == "LONG" else -raw
    return CryptoSignalForwardHorizon(minutes, True, directional)


def _excursions_r(
    *,
    side: str,
    entry_price: Decimal,
    risk_distance: Decimal,
    bars: Sequence[BybitKlineBar],
) -> tuple[Decimal | None, Decimal | None]:
    if not bars:
        return None, None
    if risk_distance <= 0:
        raise ValueError("signal event audit risk distance must be positive")
    if side == "LONG":
        favorable = max(bar.high for bar in bars) - entry_price
        adverse = min(bar.low for bar in bars) - entry_price
    elif side == "SHORT":
        favorable = entry_price - min(bar.low for bar in bars)
        adverse = entry_price - max(bar.high for bar in bars)
    else:
        raise ValueError("signal event audit side is invalid")
    return max(favorable, _ZERO) / risk_distance, max(-adverse, _ZERO) / risk_distance


def _clarity_band(ratio: Decimal) -> str:
    if ratio < _ONE:
        raise ValueError("eligible signal cannot be below the frozen quality gate")
    if ratio < Decimal("1.25"):
        return "MARGINAL"
    if ratio < Decimal("1.75"):
        return "CLEAR"
    return "STRONG"


def _summary(rows: Sequence[CryptoSignalEventOutcome]) -> dict[str, Any]:
    if not rows:
        return {
            "signal_count": 0,
            "plan_eligible_count": 0,
            "plan_eligible_rate": None,
            "plan_block_reason_counts": {},
            "median_quality_ratio": None,
            "median_mfe_r_240m": None,
            "median_mae_r_240m": None,
            "horizons": {},
        }
    plan_eligible = sum(item.plan_eligible_at_reference_equity for item in rows)
    block_reasons = Counter(
        reason for item in rows for reason in item.plan_block_reasons
    )
    mfe = [
        item.maximum_favorable_r_240m
        for item in rows
        if item.maximum_favorable_r_240m is not None
    ]
    mae = [
        item.maximum_adverse_r_240m
        for item in rows
        if item.maximum_adverse_r_240m is not None
    ]
    return {
        "signal_count": len(rows),
        "plan_eligible_count": plan_eligible,
        "plan_eligible_rate": plan_eligible / len(rows),
        "plan_block_reason_counts": dict(sorted(block_reasons.items())),
        "median_quality_ratio": float(
            statistics.median(item.quality_ratio_to_entry_gate for item in rows)
        ),
        "median_mfe_r_240m": None if not mfe else float(statistics.median(mfe)),
        "median_mae_r_240m": None if not mae else float(statistics.median(mae)),
        "horizons": {
            str(minutes): _horizon_summary(rows, minutes=minutes)
            for minutes in _HORIZONS
        },
    }


def _horizon_summary(
    rows: Sequence[CryptoSignalEventOutcome],
    *,
    minutes: int,
) -> dict[str, Any]:
    values = [
        horizon.directional_return_fraction
        for item in rows
        for horizon in item.horizons
        if horizon.minutes == minutes
        and horizon.complete
        and horizon.directional_return_fraction is not None
    ]
    if not values:
        return {
            "complete_count": 0,
            "positive_direction_count": 0,
            "positive_direction_rate": None,
            "median_directional_return_fraction": None,
            "average_directional_return_fraction": None,
        }
    positives = sum(value > _ZERO for value in values)
    return {
        "complete_count": len(values),
        "positive_direction_count": positives,
        "positive_direction_rate": positives / len(values),
        "median_directional_return_fraction": float(statistics.median(values)),
        "average_directional_return_fraction": float(
            sum(values, start=_ZERO) / Decimal(len(values))
        ),
    }


def _to_payload(item: CryptoSignalEventOutcome) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "side": item.side,
        "decision_time": item.decision_time,
        "signal_available_at": item.signal_available_at,
        "next_bar_open_time": item.next_bar_open_time,
        "entry_reference_price": float(item.entry_reference_price),
        "quality_score": float(item.quality_score),
        "quality_ratio_to_entry_gate": float(item.quality_ratio_to_entry_gate),
        "clarity_band": item.clarity_band,
        "momentum_to_atr": float(item.momentum_to_atr),
        "trend_strength_atr": float(item.trend_strength_atr),
        "breakout_strength_atr": float(item.breakout_strength_atr),
        "atr_fraction": float(item.atr_fraction),
        "one_bar_atr_multiple": float(item.one_bar_atr_multiple),
        "average_turnover_usdt": float(item.average_turnover_usdt),
        "plan_eligible_at_reference_equity": item.plan_eligible_at_reference_equity,
        "plan_block_reasons": list(item.plan_block_reasons),
        "maximum_favorable_r_240m": (
            None
            if item.maximum_favorable_r_240m is None
            else float(item.maximum_favorable_r_240m)
        ),
        "maximum_adverse_r_240m": (
            None
            if item.maximum_adverse_r_240m is None
            else float(item.maximum_adverse_r_240m)
        ),
        "horizons": [
            {
                "minutes": horizon.minutes,
                "complete": horizon.complete,
                "directional_return_fraction": (
                    None
                    if horizon.directional_return_fraction is None
                    else float(horizon.directional_return_fraction)
                ),
            }
            for horizon in item.horizons
        ],
    }


__all__ = ["audit_all_crypto_signal_events"]
