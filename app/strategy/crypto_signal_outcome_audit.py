from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.strategy.crypto_historical_diagnostics import CryptoHistoricalTradeCondition
from app.strategy.crypto_perp import CryptoPerpStrategyConfig

_ZERO = Decimal("0")
_PNL_EPSILON_USDT = Decimal("0.000001")
_Z_95 = 1.959963984540054
_PLANNED_PROFIT_EXITS = frozenset({"NET_TARGET", "PROFIT_PROTECTION"})


@dataclass(frozen=True)
class CryptoSignalOutcomeAuditPolicy:
    minimum_pattern_trades: int = 5
    sample_sufficient_trades: int = 30
    minimum_cross_symbol_count: int = 2

    def validate(self) -> None:
        if not 1 <= self.minimum_pattern_trades <= self.sample_sufficient_trades:
            raise ValueError("signal audit minimum trades must be positive and <= sufficient count")
        if self.sample_sufficient_trades > 100_000:
            raise ValueError("signal audit sufficient trade count is unreasonably large")
        if not 1 <= self.minimum_cross_symbol_count <= 1000:
            raise ValueError("signal audit minimum cross-symbol count is invalid")


def audit_crypto_signal_outcomes(
    records: Sequence[CryptoHistoricalTradeCondition],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    policy: CryptoSignalOutcomeAuditPolicy | None = None,
) -> dict[str, Any]:
    """Audit observed signal outcomes without fitting or promoting a new trading rule."""

    active = CryptoSignalOutcomeAuditPolicy() if policy is None else policy
    active.validate()
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()

    ordered = tuple(sorted(records, key=lambda item: (item.decision_time, item.symbol, item.side)))
    for record in ordered:
        _validate_record(record)

    exact_symbol_groups: dict[str, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    cross_symbol_groups: dict[str, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    symbol_groups: dict[str, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    side_groups: dict[str, list[CryptoHistoricalTradeCondition]] = defaultdict(list)
    for record in ordered:
        exact_symbol_groups[f"{record.symbol}|{record.repeated_pattern}"].append(record)
        cross_symbol_groups[record.repeated_pattern].append(record)
        symbol_groups[record.symbol].append(record)
        side_groups[record.side].append(record)

    exact_patterns = _pattern_table(
        exact_symbol_groups,
        active=active,
        minimum_symbol_count=1,
        quality_minimum=config.minimum_quality_score,
    )
    cross_patterns = _pattern_table(
        cross_symbol_groups,
        active=active,
        minimum_symbol_count=active.minimum_cross_symbol_count,
        quality_minimum=config.minimum_quality_score,
    )
    perfect_positive = [
        row
        for row in cross_patterns
        if row["observed_perfect_positive"]
        and row["minimum_support_met"]
        and row["cross_symbol_support_met"]
    ]
    perfect_planned = [
        row
        for row in cross_patterns
        if row["observed_perfect_planned_profit_exit"]
        and row["minimum_support_met"]
        and row["cross_symbol_support_met"]
    ]

    return {
        "audit": "BYBIT_CRYPTO_SIGNAL_OUTCOME_AUDIT_V2",
        "trade_count": len(ordered),
        "symbol_count": len(symbol_groups),
        "symbols": sorted(symbol_groups),
        "strategy_signal_contract": {
            "fast_ema_bars": config.fast_ema_bars,
            "slow_ema_bars": config.slow_ema_bars,
            "momentum_bars": config.momentum_bars,
            "breakout_bars": config.breakout_bars,
            "atr_bars": config.atr_bars,
            "minimum_abs_momentum": float(config.minimum_abs_momentum),
            "minimum_quality_score": float(config.minimum_quality_score),
            "minimum_average_turnover_usdt": float(config.minimum_average_turnover_usdt),
            "minimum_atr_fraction": float(config.minimum_atr_fraction),
            "maximum_atr_fraction": float(config.maximum_atr_fraction),
        },
        "aggregate": _summarize(ordered, quality_minimum=config.minimum_quality_score),
        "by_symbol": {
            key: _summarize(value, quality_minimum=config.minimum_quality_score)
            for key, value in sorted(symbol_groups.items())
        },
        "by_side": {
            key: _summarize(value, quality_minimum=config.minimum_quality_score)
            for key, value in sorted(side_groups.items())
        },
        "trade_rows": [
            _trade_row(record, quality_minimum=config.minimum_quality_score)
            for record in ordered
        ],
        "exact_symbol_patterns": exact_patterns,
        "cross_symbol_patterns": cross_patterns,
        "retrospective_perfect_positive_cross_symbol_patterns": perfect_positive,
        "retrospective_perfect_planned_profit_cross_symbol_patterns": perfect_planned,
        "perfect_positive_pattern_count": len(perfect_positive),
        "perfect_planned_profit_pattern_count": len(perfect_planned),
        "pnl_epsilon_usdt": float(_PNL_EPSILON_USDT),
        "positive_close_definition": (
            "modeled realized net_pnl_usdt > +0.000001 USDT"
        ),
        "breakeven_close_definition": (
            "absolute modeled realized net_pnl_usdt <= 0.000001 USDT"
        ),
        "loss_close_definition": (
            "modeled realized net_pnl_usdt < -0.000001 USDT"
        ),
        "planned_profit_exit_definition": (
            "modeled realized net_pnl_usdt > +0.000001 USDT and exit_reason in "
            "{NET_TARGET, PROFIT_PROTECTION}"
        ),
        "signal_clarity_definition": (
            "quality_score divided by the frozen minimum_quality_score; no new threshold is fit"
        ),
        "pattern_definition": (
            "side|volatility_regime|trend_regime|breakout_regime|turnover_regime"
        ),
        "minimum_pattern_trades": active.minimum_pattern_trades,
        "sample_sufficient_trades": active.sample_sufficient_trades,
        "minimum_cross_symbol_count": active.minimum_cross_symbol_count,
        "retrospective_only": True,
        "parameter_retuning_performed": False,
        "ranking_weights_changed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _pattern_table(
    groups: dict[str, list[CryptoHistoricalTradeCondition]],
    *,
    active: CryptoSignalOutcomeAuditPolicy,
    minimum_symbol_count: int,
    quality_minimum: Decimal,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, records in groups.items():
        summary = _summarize(records, quality_minimum=quality_minimum)
        symbol_count = len({item.symbol for item in records})
        minimum_support_met = len(records) >= active.minimum_pattern_trades
        cross_symbol_support_met = symbol_count >= minimum_symbol_count
        perfect_positive = bool(records) and summary["positive_close_count"] == len(records)
        perfect_planned = bool(records) and summary["planned_profit_exit_count"] == len(records)
        if perfect_planned and minimum_support_met and cross_symbol_support_met:
            tier = (
                "RETROSPECTIVE_PERFECT_PLANNED_SAMPLE_SUFFICIENT"
                if len(records) >= active.sample_sufficient_trades
                else "RETROSPECTIVE_PERFECT_PLANNED_SMALL_SAMPLE"
            )
        elif perfect_positive and minimum_support_met and cross_symbol_support_met:
            tier = (
                "RETROSPECTIVE_PERFECT_POSITIVE_SAMPLE_SUFFICIENT"
                if len(records) >= active.sample_sufficient_trades
                else "RETROSPECTIVE_PERFECT_POSITIVE_SMALL_SAMPLE"
            )
        elif minimum_support_met and cross_symbol_support_met:
            tier = "RETROSPECTIVE_MIXED"
        else:
            tier = "INSUFFICIENT_SUPPORT"
        result.append(
            {
                "pattern": key,
                "symbol_count": symbol_count,
                "symbols": sorted({item.symbol for item in records}),
                "minimum_support_met": minimum_support_met,
                "cross_symbol_support_met": cross_symbol_support_met,
                "sample_sufficient": len(records) >= active.sample_sufficient_trades,
                "observed_perfect_positive": perfect_positive,
                "observed_perfect_planned_profit_exit": perfect_planned,
                "candidate_tier": tier,
                **summary,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            bool(item["observed_perfect_planned_profit_exit"]),
            bool(item["observed_perfect_positive"]),
            bool(item["sample_sufficient"]),
            float(item["planned_profit_exit_rate"] or 0.0),
            float(item["positive_close_rate"] or 0.0),
            int(item["trade_count"]),
            float(item["total_net_pnl_usdt"]),
            str(item["pattern"]),
        ),
        reverse=True,
    )


def _summarize(
    records: Sequence[CryptoHistoricalTradeCondition],
    *,
    quality_minimum: Decimal,
) -> dict[str, Any]:
    rows = tuple(records)
    if not rows:
        return {
            "trade_count": 0,
            "positive_close_count": 0,
            "breakeven_close_count": 0,
            "loss_close_count": 0,
            "non_positive_close_count": 0,
            "positive_close_rate": None,
            "positive_rate_wilson_lower_95": None,
            "planned_profit_exit_count": 0,
            "planned_profit_exit_rate": None,
            "planned_profit_rate_wilson_lower_95": None,
            "total_net_pnl_usdt": 0.0,
            "average_net_pnl_usdt": None,
            "median_net_pnl_usdt": None,
            "profit_factor": None,
            "median_mfe_r": None,
            "median_mae_r": None,
            "median_holding_bars": None,
            "minimum_quality_score": None,
            "median_quality_score": None,
            "minimum_quality_ratio_to_entry_gate": None,
            "median_quality_ratio_to_entry_gate": None,
            "exit_reason_counts": {},
        }

    positives = [item for item in rows if item.net_pnl_usdt > _PNL_EPSILON_USDT]
    breakeven = [
        item for item in rows if abs(item.net_pnl_usdt) <= _PNL_EPSILON_USDT
    ]
    negative = [item for item in rows if item.net_pnl_usdt < -_PNL_EPSILON_USDT]
    planned = [
        item
        for item in positives
        if item.exit_reason in _PLANNED_PROFIT_EXITS
    ]
    gains = sum((item.net_pnl_usdt for item in positives), start=_ZERO)
    losses = -sum((item.net_pnl_usdt for item in negative), start=_ZERO)
    total = sum((item.net_pnl_usdt for item in rows), start=_ZERO)
    qualities = [item.quality_score for item in rows]
    quality_ratios = [value / quality_minimum for value in qualities]
    return {
        "trade_count": len(rows),
        "positive_close_count": len(positives),
        "breakeven_close_count": len(breakeven),
        "loss_close_count": len(negative),
        "non_positive_close_count": len(rows) - len(positives),
        "positive_close_rate": len(positives) / len(rows),
        "positive_rate_wilson_lower_95": _wilson_lower(len(positives), len(rows)),
        "planned_profit_exit_count": len(planned),
        "planned_profit_exit_rate": len(planned) / len(rows),
        "planned_profit_rate_wilson_lower_95": _wilson_lower(len(planned), len(rows)),
        "total_net_pnl_usdt": float(total),
        "average_net_pnl_usdt": float(total / Decimal(len(rows))),
        "median_net_pnl_usdt": float(statistics.median(item.net_pnl_usdt for item in rows)),
        "profit_factor": None if losses == _ZERO else float(gains / losses),
        "median_mfe_r": float(statistics.median(item.maximum_favorable_r for item in rows)),
        "median_mae_r": float(statistics.median(item.maximum_adverse_r for item in rows)),
        "median_holding_bars": float(statistics.median(item.holding_bars for item in rows)),
        "minimum_quality_score": float(min(qualities)),
        "median_quality_score": float(statistics.median(qualities)),
        "minimum_quality_ratio_to_entry_gate": float(min(quality_ratios)),
        "median_quality_ratio_to_entry_gate": float(statistics.median(quality_ratios)),
        "exit_reason_counts": dict(sorted(Counter(item.exit_reason for item in rows).items())),
    }


def _trade_row(
    record: CryptoHistoricalTradeCondition,
    *,
    quality_minimum: Decimal,
) -> dict[str, Any]:
    positive = record.net_pnl_usdt > _PNL_EPSILON_USDT
    loss = record.net_pnl_usdt < -_PNL_EPSILON_USDT
    outcome = "WIN" if positive else "LOSS" if loss else "BREAKEVEN"
    return {
        "symbol": record.symbol,
        "side": record.side,
        "decision_time": record.decision_time,
        "entry_time": record.entry_time,
        "exit_time": record.exit_time,
        "exit_reason": record.exit_reason,
        "net_pnl_usdt": float(record.net_pnl_usdt),
        "economic_outcome": outcome,
        "positive_close": positive,
        "breakeven_close": not positive and not loss,
        "loss_close": loss,
        "planned_profit_exit": (
            positive and record.exit_reason in _PLANNED_PROFIT_EXITS
        ),
        "quality_score": float(record.quality_score),
        "quality_ratio_to_entry_gate": float(record.quality_score / quality_minimum),
        "quality_margin_above_entry_gate": float(record.quality_score - quality_minimum),
        "momentum_to_atr": float(record.momentum_to_atr),
        "trend_strength_atr": float(record.trend_strength_atr),
        "breakout_strength_atr": float(record.breakout_strength_atr),
        "atr_fraction": float(record.atr_fraction),
        "one_bar_atr_multiple": float(record.one_bar_atr_multiple),
        "average_turnover_usdt": float(record.average_turnover_usdt),
        "expected_net_edge_usd": float(record.expected_net_edge_usd),
        "maximum_favorable_r": float(record.maximum_favorable_r),
        "maximum_adverse_r": float(record.maximum_adverse_r),
        "holding_bars": record.holding_bars,
        "pattern": record.repeated_pattern,
    }


def _wilson_lower(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = _Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


def _validate_record(record: CryptoHistoricalTradeCondition) -> None:
    if record.side not in {"LONG", "SHORT"}:
        raise ValueError("signal audit record side is invalid")
    if not record.symbol or record.symbol != record.symbol.strip().upper():
        raise ValueError("signal audit record symbol is invalid")
    for name, value in (
        ("quality_score", record.quality_score),
        ("net_pnl_usdt", record.net_pnl_usdt),
        ("maximum_favorable_r", record.maximum_favorable_r),
        ("maximum_adverse_r", record.maximum_adverse_r),
    ):
        if not value.is_finite():
            raise ValueError(f"signal audit record {name} must be finite")
    if record.holding_bars < 0:
        raise ValueError("signal audit record holding bars cannot be negative")


__all__ = ["CryptoSignalOutcomeAuditPolicy", "audit_crypto_signal_outcomes"]
