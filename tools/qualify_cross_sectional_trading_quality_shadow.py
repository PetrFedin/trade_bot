from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    CrossSectionalPortfolioResult,
)
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelector,
    SelectionQualityPolicy,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.position_sizing import RiskAwareSizingPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from app.strategy.regime_momentum import RegimeAwareMomentumConfig

_SCHEMA = "cross-sectional-trading-quality-shadow-v2"
_ABLATIONS = ("SELECTION_ONLY", "SIZING_ONLY", "PROTECTION_ONLY", "COMBINED")


def _object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _decimal(data: dict[str, Any], field: str) -> Decimal:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _string_list(data: dict[str, Any], field: str) -> list[str]:
    value = data.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
        raise ValueError("trading-quality shadow schema mismatch")
    if data.get("shadow_only") is not True:
        raise ValueError("trading-quality candidate must remain shadow-only")
    if data.get("strategy_promotion_allowed") is not False:
        raise ValueError("trading-quality candidate cannot allow promotion")
    if _positive_int(data, "minimum_symbols") < 3:
        raise ValueError("trading-quality research requires at least three symbols")
    for field in ("opening_cash", "fee_per_fill", "slippage_bps"):
        _decimal(data, field)

    selection = _object(data, "selection")
    top_k = _positive_int(selection, "top_k")
    quality = _object(selection, "quality")
    for field in (
        "momentum_weight",
        "trend_weight",
        "volatility_penalty_weight",
    ):
        _decimal(quality, field)
    signal = _object(selection, "signal")
    for field in (
        "fast_bars",
        "slow_bars",
        "momentum_lookback_bars",
        "volatility_bars",
    ):
        _positive_int(signal, field)
    for field in (
        "minimum_momentum_return",
        "minimum_trend_strength",
        "maximum_realized_volatility",
    ):
        _decimal(signal, field)

    portfolio = _object(data, "portfolio")
    maximum_gross = _decimal(portfolio, "maximum_gross_exposure_fraction")
    legacy_target = _decimal(
        portfolio, "legacy_new_position_target_equity_fraction"
    )
    if legacy_target * Decimal(top_k) > maximum_gross:
        raise ValueError("legacy target allocation exceeds admission gross cap")
    if portfolio.get("allow_leverage") is not False:
        raise ValueError("trading-quality research must keep leverage disabled")
    if portfolio.get("rebalance_existing_positions") is not False:
        raise ValueError("trading-quality research cannot rebalance existing positions")

    sizing = _object(data, "risk_aware_sizing")
    for field in (
        "risk_budget_fraction",
        "maximum_equity_fraction",
        "target_realized_volatility",
        "volatility_floor",
    ):
        _decimal(sizing, field)

    for section in (
        "legacy_position_management",
        "candidate_position_management",
    ):
        position = _object(data, section)
        for field in (
            "stop_loss_fraction",
            "take_profit_fraction",
            "trailing_activation_fraction",
            "trailing_stop_fraction",
        ):
            _decimal(position, field)
        _positive_int(position, "maximum_holding_bars")
    candidate = _object(data, "candidate_position_management")
    for field in (
        "break_even_activation_fraction",
        "break_even_buffer_fraction",
        "profit_protection_activation_fraction",
        "maximum_profit_giveback_fraction",
    ):
        _decimal(candidate, field)

    reentry = _object(data, "reentry_confirmation")
    _positive_int(reentry, "minimum_consecutive_eligible_bars")
    if reentry.get("initial_entry_requires_confirmation") is not False:
        raise ValueError("initial entry confirmation policy changed")
    if reentry.get("reset_streak_on_ineligible_signal") is not True:
        raise ValueError("reentry streak reset policy changed")
    if reentry.get("apply_after_any_exit") is not True:
        raise ValueError("reentry application policy changed")

    _string_list(data, "shared_controls")
    _string_list(data, "candidate_components")
    if tuple(_string_list(data, "ablation_variants")) != _ABLATIONS:
        raise ValueError("trading-quality ablation set changed")
    _string_list(data, "promotion_blockers")
    _string_list(data, "limitations")
    return data


def _signal_config(config: dict[str, Any]) -> RegimeAwareMomentumConfig:
    signal = _object(_object(config, "selection"), "signal")
    return RegimeAwareMomentumConfig(
        fast_bars=_positive_int(signal, "fast_bars"),
        slow_bars=_positive_int(signal, "slow_bars"),
        momentum_lookback_bars=_positive_int(signal, "momentum_lookback_bars"),
        volatility_bars=_positive_int(signal, "volatility_bars"),
        minimum_momentum_return=_decimal(signal, "minimum_momentum_return"),
        minimum_trend_strength=_decimal(signal, "minimum_trend_strength"),
        maximum_realized_volatility=_decimal(signal, "maximum_realized_volatility"),
    )


def _quality_policy(config: dict[str, Any]) -> SelectionQualityPolicy:
    quality = _object(_object(config, "selection"), "quality")
    return SelectionQualityPolicy(
        momentum_weight=_decimal(quality, "momentum_weight"),
        trend_weight=_decimal(quality, "trend_weight"),
        volatility_penalty_weight=_decimal(quality, "volatility_penalty_weight"),
    )


def _portfolio_policy(config: dict[str, Any]) -> CrossSectionalPortfolioPolicy:
    portfolio = _object(config, "portfolio")
    return CrossSectionalPortfolioPolicy(
        opening_cash=_decimal(config, "opening_cash"),
        fee_per_fill=_decimal(config, "fee_per_fill"),
        slippage_bps=_decimal(config, "slippage_bps"),
        maximum_gross_exposure_fraction=_decimal(
            portfolio, "maximum_gross_exposure_fraction"
        ),
        new_position_target_equity_fraction=_decimal(
            portfolio, "legacy_new_position_target_equity_fraction"
        ),
        allow_leverage=False,
        rebalance_existing_positions=False,
    )


def _position_policy(
    config: dict[str, Any], *, protection: bool
) -> PositionManagementPolicy:
    section = (
        "candidate_position_management" if protection else "legacy_position_management"
    )
    position = _object(config, section)
    kwargs: dict[str, Any] = {
        "stop_loss_fraction": _decimal(position, "stop_loss_fraction"),
        "take_profit_fraction": _decimal(position, "take_profit_fraction"),
        "trailing_activation_fraction": _decimal(
            position, "trailing_activation_fraction"
        ),
        "trailing_stop_fraction": _decimal(position, "trailing_stop_fraction"),
        "maximum_holding_bars": _positive_int(position, "maximum_holding_bars"),
    }
    if protection:
        kwargs.update(
            break_even_activation_fraction=_decimal(
                position, "break_even_activation_fraction"
            ),
            break_even_buffer_fraction=_decimal(
                position, "break_even_buffer_fraction"
            ),
            profit_protection_activation_fraction=_decimal(
                position, "profit_protection_activation_fraction"
            ),
            maximum_profit_giveback_fraction=_decimal(
                position, "maximum_profit_giveback_fraction"
            ),
        )
    return PositionManagementPolicy(**kwargs)


def _sizing_policy(config: dict[str, Any]) -> RiskAwareSizingPolicy:
    sizing = _object(config, "risk_aware_sizing")
    return RiskAwareSizingPolicy(
        risk_budget_fraction=_decimal(sizing, "risk_budget_fraction"),
        maximum_equity_fraction=_decimal(sizing, "maximum_equity_fraction"),
        target_realized_volatility=_decimal(sizing, "target_realized_volatility"),
        volatility_floor=_decimal(sizing, "volatility_floor"),
    )


def _reentry_policy(config: dict[str, Any]) -> ReentryConfirmationPolicy:
    reentry = _object(config, "reentry_confirmation")
    return ReentryConfirmationPolicy(
        minimum_consecutive_eligible_bars=_positive_int(
            reentry, "minimum_consecutive_eligible_bars"
        ),
        initial_entry_requires_confirmation=False,
        reset_streak_on_ineligible_signal=True,
        apply_after_any_exit=True,
    )


def read_csv(path: Path) -> tuple[OhlcvBar, ...]:
    bars: list[OhlcvBar] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = {
            "symbol",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
        }
        if reader.fieldnames is None or set(reader.fieldnames) != expected:
            raise ValueError("trading-quality CSV schema mismatch")
        for row in reader:
            vwap_raw = row["vwap"].strip()
            bar = OhlcvBar(
                symbol=row["symbol"].strip(),
                timestamp=datetime.fromisoformat(row["timestamp"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=int(row["volume"]),
                trade_count=int(row["trade_count"]),
                vwap=None if not vwap_raw else Decimal(vwap_raw),
            )
            bar.validate()
            bars.append(bar)
    if not bars:
        raise ValueError("trading-quality CSV is empty")
    return tuple(bars)


def synchronize_common_timestamps(
    bars: tuple[OhlcvBar, ...], *, minimum_symbols: int
) -> tuple[OhlcvBar, ...]:
    by_symbol: defaultdict[str, list[OhlcvBar]] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar)
    if len(by_symbol) < minimum_symbols:
        raise ValueError("insufficient symbols for trading-quality comparison")
    timestamp_sets = [
        {bar.timestamp for bar in symbol_bars} for symbol_bars in by_symbol.values()
    ]
    common = set.intersection(*timestamp_sets)
    if not common:
        raise ValueError("no common timestamps across trading-quality symbols")
    synchronized = tuple(
        sorted(
            (bar for bar in bars if bar.timestamp in common),
            key=lambda item: (item.symbol, item.timestamp),
        )
    )
    counts = defaultdict(int)
    for bar in synchronized:
        counts[bar.symbol] += 1
    if len(set(counts.values())) != 1:
        raise RuntimeError("common-timestamp synchronization drifted")
    return synchronized


def _serialize(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _result_metrics(result: CrossSectionalPortfolioResult) -> dict[str, object]:
    return {
        "fill_count": result.fill_count,
        "closed_trade_count": result.closed_trade_count,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "breakeven_trades": result.breakeven_trades,
        "win_rate": str(result.win_rate),
        "profit_factor": _serialize(result.profit_factor),
        "total_pnl": str(result.total_pnl),
        "total_return": str(result.total_return),
        "max_drawdown_fraction": str(result.max_drawdown_fraction),
        "turnover_fraction": str(result.turnover_fraction),
        "fees_paid": str(result.fees_paid),
        "maximum_gross_exposure_fraction_observed": str(
            result.maximum_gross_exposure_fraction_observed
        ),
        "maximum_concurrent_positions": result.maximum_concurrent_positions,
        "average_maximum_favorable_excursion_fraction": str(
            result.average_maximum_favorable_excursion_fraction
        ),
        "average_maximum_adverse_excursion_fraction": str(
            result.average_maximum_adverse_excursion_fraction
        ),
        "average_mfe_capture_ratio": _serialize(result.average_mfe_capture_ratio),
        "positive_mfe_trades": result.positive_mfe_trades,
        "positive_mfe_closed_profitable": result.positive_mfe_closed_profitable,
        "positive_mfe_closed_losing_or_flat": result.positive_mfe_closed_losing_or_flat,
        "profit_preservation_rate": _serialize(result.profit_preservation_rate),
        "selection_counts": result.selection_counts,
        "intrabar_exit_counts": result.intrabar_exit_counts,
        "entry_block_counts": result.entry_block_counts,
    }


def _delta(candidate: str | None, control: str | None) -> str | None:
    if candidate is None or control is None:
        return None
    return str(Decimal(candidate) - Decimal(control))


def _comparison(
    candidate: dict[str, object], control: dict[str, object]
) -> dict[str, str | None]:
    return {
        "total_return_delta": _delta(
            candidate["total_return"], control["total_return"]
        ),
        "max_drawdown_fraction_delta": _delta(
            candidate["max_drawdown_fraction"], control["max_drawdown_fraction"]
        ),
        "win_rate_delta": _delta(candidate["win_rate"], control["win_rate"]),
        "profit_factor_delta": _delta(
            candidate["profit_factor"], control["profit_factor"]
        ),
        "profit_preservation_rate_delta": _delta(
            candidate["profit_preservation_rate"],
            control["profit_preservation_rate"],
        ),
        "average_mfe_capture_ratio_delta": _delta(
            candidate["average_mfe_capture_ratio"],
            control["average_mfe_capture_ratio"],
        ),
    }


def _run_variant(
    *,
    bars: tuple[OhlcvBar, ...],
    config: dict[str, Any],
    use_quality_selection: bool,
    use_risk_sizing: bool,
    use_profit_protection: bool,
) -> CrossSectionalPortfolioResult:
    selection = _object(config, "selection")
    selector = CrossSectionalSelector(
        top_k=_positive_int(selection, "top_k"),
        signal_config=_signal_config(config),
        quality_policy=_quality_policy(config) if use_quality_selection else None,
    )
    return CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=_portfolio_policy(config),
        position_policy=_position_policy(
            config, protection=use_profit_protection
        ),
        reentry_policy=_reentry_policy(config),
        sizing_policy=_sizing_policy(config) if use_risk_sizing else None,
    ).run(bars)


def qualify(csv_path: Path, config_path: Path) -> dict[str, object]:
    config = load_config(config_path)
    raw_bars = read_csv(csv_path)
    minimum_symbols = _positive_int(config, "minimum_symbols")
    bars = synchronize_common_timestamps(raw_bars, minimum_symbols=minimum_symbols)
    symbols = sorted({bar.symbol for bar in bars})
    common_timestamps = sorted({bar.timestamp for bar in bars})

    signal_config = _signal_config(config)
    if len(common_timestamps) <= signal_config.minimum_history_bars:
        raise ValueError("insufficient synchronized history for portfolio comparison")

    variants = {
        "CONTROL": _run_variant(
            bars=bars,
            config=config,
            use_quality_selection=False,
            use_risk_sizing=False,
            use_profit_protection=False,
        ),
        "SELECTION_ONLY": _run_variant(
            bars=bars,
            config=config,
            use_quality_selection=True,
            use_risk_sizing=False,
            use_profit_protection=False,
        ),
        "SIZING_ONLY": _run_variant(
            bars=bars,
            config=config,
            use_quality_selection=False,
            use_risk_sizing=True,
            use_profit_protection=False,
        ),
        "PROTECTION_ONLY": _run_variant(
            bars=bars,
            config=config,
            use_quality_selection=False,
            use_risk_sizing=False,
            use_profit_protection=True,
        ),
        "COMBINED": _run_variant(
            bars=bars,
            config=config,
            use_quality_selection=True,
            use_risk_sizing=True,
            use_profit_protection=True,
        ),
    }
    metrics = {name: _result_metrics(result) for name, result in variants.items()}
    control = metrics["CONTROL"]
    candidate = metrics["COMBINED"]
    ablations = {name: metrics[name] for name in _ABLATIONS[:-1]}
    ablation_deltas = {
        name: _comparison(metrics[name], control) for name in _ABLATIONS[:-1]
    }
    admission_cap = _decimal(
        _object(config, "portfolio"), "maximum_gross_exposure_fraction"
    )

    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_COMPARATIVE_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": symbols,
        "synchronized_timestamp_count": len(common_timestamps),
        "first_timestamp": common_timestamps[0].isoformat(),
        "last_timestamp": common_timestamps[-1].isoformat(),
        "admission_gross_exposure_cap_fraction": str(admission_cap),
        "observed_gross_may_drift_above_admission_cap": True,
        "shared_controls": list(config["shared_controls"]),
        "candidate_components": list(config["candidate_components"]),
        "ablation_evidence_available": True,
        "component_attribution_complete": False,
        "control": control,
        "candidate": candidate,
        "ablations": ablations,
        "comparison": _comparison(candidate, control),
        "ablation_deltas_vs_control": ablation_deltas,
        "limitations": list(config["limitations"]),
        "promotion_blockers": list(config["promotion_blockers"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare trading-quality components and combined shadow vs legacy"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.validate_config_only:
        print(
            json.dumps(
                {
                    "schema_version": _SCHEMA,
                    "config_valid": True,
                    "shadow_only": config["shadow_only"],
                    "strategy_promotion_allowed": config[
                        "strategy_promotion_allowed"
                    ],
                    "ablation_variants": config["ablation_variants"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.csv is None or args.output is None:
        raise ValueError("csv and output are required for comparative qualification")
    evidence = qualify(args.csv, args.config)
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
