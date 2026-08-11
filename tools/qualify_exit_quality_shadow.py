from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.historical import HistoricalDataPolicy
from app.marketdata.manifest import ManifestedCsvHistoricalBarSource
from app.strategy.backtest import BacktestConfig
from app.strategy.managed_backtest import ManagedBacktestResult, ManagedHistoricalBacktester
from app.strategy.position_management import ExitReason, PositionManagementPolicy
from app.strategy.regime_momentum import (
    RegimeAwareMomentumConfig,
    RegimeAwareMomentumStrategy,
)

_POLICY_SCHEMA = "strategy-quality-shadow-v1"
_EVIDENCE_SCHEMA = "exit-quality-shadow-v1"


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
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _POLICY_SCHEMA:
        raise ValueError("strategy quality shadow policy schema mismatch")
    if data.get("shadow_only") is not True or data.get("promotion_allowed") is not False:
        raise ValueError("exit quality evidence requires non-promotable shadow policy")
    return data


def _serialize_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _candidate_from_policy(
    policy: dict[str, Any],
) -> tuple[RegimeAwareMomentumStrategy, PositionManagementPolicy, BacktestConfig, int]:
    signal = _object(policy, "signal")
    position = _object(policy, "position_management")
    target_quantity = _decimal(policy, "target_quantity")
    strategy = RegimeAwareMomentumStrategy(
        strategy_id=str(policy["strategy_id"]),
        target_quantity=target_quantity,
        config=RegimeAwareMomentumConfig(
            fast_bars=_positive_int(signal, "fast_bars"),
            slow_bars=_positive_int(signal, "slow_bars"),
            momentum_lookback_bars=_positive_int(signal, "momentum_lookback_bars"),
            volatility_bars=_positive_int(signal, "volatility_bars"),
            minimum_momentum_return=_decimal(signal, "minimum_momentum_return"),
            minimum_trend_strength=_decimal(signal, "minimum_trend_strength"),
            maximum_realized_volatility=_decimal(signal, "maximum_realized_volatility"),
        ),
    )
    management = PositionManagementPolicy(
        stop_loss_fraction=_decimal(position, "stop_loss_fraction"),
        take_profit_fraction=_decimal(position, "take_profit_fraction"),
        trailing_activation_fraction=_decimal(position, "trailing_activation_fraction"),
        trailing_stop_fraction=_decimal(position, "trailing_stop_fraction"),
        maximum_holding_bars=_positive_int(position, "maximum_holding_bars"),
    )
    backtest = BacktestConfig(
        opening_cash=_decimal(policy, "opening_cash"),
        fee_per_fill=_decimal(policy, "fee_per_fill"),
        slippage_bps=_decimal(policy, "slippage_bps"),
    )
    return strategy, management, backtest, _positive_int(policy, "training_bars")


def _result_evidence(result: ManagedBacktestResult) -> dict[str, object]:
    action_counts = Counter(item.action.value for item in result.decision_trace)
    rejection_counts = Counter(
        reason
        for item in result.decision_trace
        if not item.signal_eligible
        for reason in item.signal_reasons
    )
    exit_reason_counts = Counter(trade.exit_reason.value for trade in result.closed_trades)
    givebacks = [
        trade.mfe_giveback_fraction
        for trade in result.closed_trades
        if trade.mfe_giveback_fraction is not None
    ]
    override_count = sum(
        item.action.value == "EXIT"
        and item.signal_eligible
        and item.exit_reason not in (None, ExitReason.SIGNAL_EXIT)
        for item in result.decision_trace
    )
    signal_exit_count = exit_reason_counts.get(ExitReason.SIGNAL_EXIT.value, 0)
    managed_exit_count = result.closed_trade_count - signal_exit_count
    return {
        "closed_trade_count": result.closed_trade_count,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": str(result.win_rate),
        "profit_factor": _serialize_decimal(result.profit_factor),
        "average_maximum_favorable_excursion_fraction": str(
            result.average_maximum_favorable_excursion_fraction
        ),
        "average_maximum_adverse_excursion_fraction": str(
            result.average_maximum_adverse_excursion_fraction
        ),
        "average_mfe_capture_ratio": _serialize_decimal(
            result.average_mfe_capture_ratio
        ),
        "average_mfe_giveback_fraction": (
            _serialize_decimal(_mean([value for value in givebacks if value is not None]))
            if givebacks
            else None
        ),
        "trades_with_mfe_giveback": sum(
            value is not None and value > 0 for value in givebacks
        ),
        "positive_mfe_trades": result.positive_mfe_trades,
        "positive_mfe_closed_profitable": result.positive_mfe_closed_profitable,
        "positive_mfe_closed_losing_or_flat": (
            result.positive_mfe_closed_losing_or_flat
        ),
        "profit_preservation_rate": _serialize_decimal(
            result.profit_preservation_rate
        ),
        "signal_exit_count": signal_exit_count,
        "managed_exit_count": managed_exit_count,
        "decision_action_counts": dict(sorted(action_counts.items())),
        "signal_rejection_reason_counts": dict(sorted(rejection_counts.items())),
        "exit_reason_counts": dict(sorted(exit_reason_counts.items())),
        "position_manager_exit_overrides": override_count,
        "closed_trades": [
            {
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "entry_price": str(trade.entry_price),
                "exit_price": str(trade.exit_price),
                "net_pnl": str(trade.net_pnl),
                "holding_bars": trade.holding_bars,
                "exit_reason": trade.exit_reason.value,
                "maximum_favorable_excursion_fraction": str(
                    trade.maximum_favorable_excursion_fraction
                ),
                "maximum_adverse_excursion_fraction": str(
                    trade.maximum_adverse_excursion_fraction
                ),
                "mfe_capture_ratio": _serialize_decimal(trade.mfe_capture_ratio),
                "mfe_giveback_fraction": _serialize_decimal(
                    trade.mfe_giveback_fraction
                ),
            }
            for trade in result.closed_trades
        ],
        "decision_trace": [
            {
                "execution_index": item.execution_index,
                "decision_time": item.decision_time.isoformat(),
                "execution_time": item.execution_time.isoformat(),
                "action": item.action.value,
                "signal_eligible": item.signal_eligible,
                "signal_reasons": list(item.signal_reasons),
                "signal_target_quantity": str(item.signal_target_quantity),
                "final_target_quantity": str(item.final_target_quantity),
                "current_quantity": str(item.current_quantity),
                "decision_reference_price": str(item.decision_reference_price),
                "momentum_return": str(item.momentum_return),
                "trend_strength": str(item.trend_strength),
                "realized_volatility": str(item.realized_volatility),
                "position_profit_fraction": _serialize_decimal(
                    item.position_profit_fraction
                ),
                "drawdown_from_peak_fraction": _serialize_decimal(
                    item.drawdown_from_peak_fraction
                ),
                "exit_reason": None if item.exit_reason is None else item.exit_reason.value,
            }
            for item in result.decision_trace
        ],
    }


def qualify(manifest_path: Path, policy_path: Path) -> dict[str, object]:
    policy = _load_policy(policy_path)
    strategy, management, backtest, training_bars = _candidate_from_policy(policy)
    manifested = ManifestedCsvHistoricalBarSource(
        manifest_path,
        policy=HistoricalDataPolicy(
            minimum_bars=60,
            maximum_jump_fraction=Decimal("0.25"),
        ),
    ).load()

    regimes: list[dict[str, object]] = []
    all_trades = []
    all_decisions = []
    for window in manifested.manifest.windows:
        bars = [
            bar
            for bar in manifested.dataset.bars
            if window.start <= bar.timestamp < window.end
        ]
        result = ManagedHistoricalBacktester(
            strategy=strategy,
            position_policy=management,
            config=backtest,
        ).run(bars, first_execution_index=training_bars)
        all_trades.extend(result.closed_trades)
        all_decisions.extend(result.decision_trace)
        regimes.append(
            {
                "name": window.name,
                "bars": len(bars),
                "training_bars": training_bars,
                "holdout_bars": len(bars) - training_bars,
                **_result_evidence(result),
            }
        )

    action_counts = Counter(item.action.value for item in all_decisions)
    rejection_counts = Counter(
        reason
        for item in all_decisions
        if not item.signal_eligible
        for reason in item.signal_reasons
    )
    exit_reason_counts = Counter(trade.exit_reason.value for trade in all_trades)
    capture_ratios = [
        trade.mfe_capture_ratio
        for trade in all_trades
        if trade.mfe_capture_ratio is not None
    ]
    giveback_ratios = [
        trade.mfe_giveback_fraction
        for trade in all_trades
        if trade.mfe_giveback_fraction is not None
    ]
    positive_mfe = [
        trade for trade in all_trades if trade.maximum_favorable_excursion_fraction > 0
    ]
    positive_mfe_profitable = sum(trade.net_pnl > 0 for trade in positive_mfe)
    profitable_opportunity_lost = sum(trade.net_pnl <= 0 for trade in positive_mfe)
    trades_with_giveback = sum(
        value is not None and value > 0 for value in giveback_ratios
    )
    zero_mfe_losing_trades = sum(
        trade.net_pnl < 0 and trade.maximum_favorable_excursion_fraction == 0
        for trade in all_trades
    )
    signal_exit_count = exit_reason_counts.get(ExitReason.SIGNAL_EXIT.value, 0)
    managed_exit_count = len(all_trades) - signal_exit_count
    override_count = sum(
        item.action.value == "EXIT"
        and item.signal_eligible
        and item.exit_reason not in (None, ExitReason.SIGNAL_EXIT)
        for item in all_decisions
    )
    findings: list[str] = []
    if managed_exit_count == 0 and all_trades:
        findings.append("POSITION_MANAGER_NOT_TRIGGERED_IN_SAMPLE")
    if trades_with_giveback > 0:
        findings.append("PROFIT_GIVEBACK_OBSERVED")
    if zero_mfe_losing_trades > 0:
        findings.append("LOSING_TRADE_WITH_ZERO_POSITIVE_MFE")

    return {
        "schema_version": _EVIDENCE_SCHEMA,
        "shadow_only": True,
        "promotion_allowed": False,
        "dataset_id": manifested.dataset.dataset_id,
        "dataset_sha256": manifested.dataset.canonical_sha256,
        "manifest_sha256": manifested.manifest_sha256,
        "source_classification": manifested.manifest.source_classification,
        "strategy_id": strategy.strategy_id,
        "limitations": [
            "CLOSE_ONLY_EXCURSION_MEASUREMENT",
            "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE",
            "SINGLE_SYMBOL_HISTORICAL_EVIDENCE",
            "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE",
        ],
        "observed_findings": findings,
        "aggregate": {
            "decision_count": len(all_decisions),
            "decision_action_counts": dict(sorted(action_counts.items())),
            "signal_rejection_reason_counts": dict(sorted(rejection_counts.items())),
            "exit_reason_counts": dict(sorted(exit_reason_counts.items())),
            "position_manager_exit_overrides": override_count,
            "signal_exit_count": signal_exit_count,
            "managed_exit_count": managed_exit_count,
            "closed_trade_count": len(all_trades),
            "winning_trades": sum(trade.net_pnl > 0 for trade in all_trades),
            "losing_trades": sum(trade.net_pnl < 0 for trade in all_trades),
            "zero_mfe_losing_trades": zero_mfe_losing_trades,
            "average_maximum_favorable_excursion_fraction": str(
                _mean(
                    [
                        trade.maximum_favorable_excursion_fraction
                        for trade in all_trades
                    ]
                )
            ),
            "average_maximum_adverse_excursion_fraction": str(
                _mean(
                    [trade.maximum_adverse_excursion_fraction for trade in all_trades]
                )
            ),
            "average_mfe_capture_ratio": (
                _serialize_decimal(_mean(capture_ratios)) if capture_ratios else None
            ),
            "average_mfe_giveback_fraction": (
                _serialize_decimal(_mean(giveback_ratios)) if giveback_ratios else None
            ),
            "trades_with_mfe_giveback": trades_with_giveback,
            "positive_mfe_trades": len(positive_mfe),
            "positive_mfe_closed_profitable": positive_mfe_profitable,
            "positive_mfe_closed_losing_or_flat": profitable_opportunity_lost,
            "profit_preservation_rate": (
                str(Decimal(positive_mfe_profitable) / Decimal(len(positive_mfe)))
                if positive_mfe
                else None
            ),
            "profit_giveback_observed": trades_with_giveback > 0,
        },
        "regimes": regimes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate close-only exit-quality shadow evidence"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    evidence = qualify(args.manifest, args.policy)
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
