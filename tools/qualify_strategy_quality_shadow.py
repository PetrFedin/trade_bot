from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.historical import HistoricalDataPolicy
from app.marketdata.manifest import ManifestedCsvHistoricalBarSource
from app.strategy.backtest import BacktestConfig, HistoricalBacktester
from app.strategy.managed_backtest import ManagedBacktestResult, ManagedHistoricalBacktester
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.regime_momentum import (
    RegimeAwareMomentumConfig,
    RegimeAwareMomentumStrategy,
)

_SCHEMA = "strategy-quality-shadow-v1"


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


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
        raise ValueError("strategy quality shadow policy schema mismatch")
    if data.get("shadow_only") is not True:
        raise ValueError("strategy quality candidate must remain shadow_only")
    if data.get("promotion_allowed") is not False:
        raise ValueError("strategy quality shadow policy cannot allow promotion")
    if data.get("control_strategy_id") != "paper-momentum-v1":
        raise ValueError("strategy quality shadow control strategy mismatch")
    _positive_int(data, "training_bars")
    signal = _object(data, "signal")
    position = _object(data, "position_management")
    _positive_int(signal, "fast_bars")
    _positive_int(signal, "slow_bars")
    _positive_int(signal, "momentum_lookback_bars")
    _positive_int(signal, "volatility_bars")
    _positive_int(position, "maximum_holding_bars")
    return data


def _serialize_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _closed_trade_evidence(result: ManagedBacktestResult) -> dict[str, object]:
    reasons = Counter(trade.exit_reason.value for trade in result.closed_trades)
    return {
        "closed_trade_count": result.closed_trade_count,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "breakeven_trades": result.breakeven_trades,
        "win_rate": str(result.win_rate),
        "gross_profit": str(result.gross_profit),
        "gross_loss": str(result.gross_loss),
        "profit_factor": _serialize_decimal(result.profit_factor),
        "average_closed_trade_pnl": str(result.average_closed_trade_pnl),
        "exit_reason_counts": dict(sorted(reasons.items())),
        "closed_trades": [
            {
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "entry_price": str(trade.entry_price),
                "exit_price": str(trade.exit_price),
                "quantity": str(trade.quantity),
                "net_pnl": str(trade.net_pnl),
                "return_fraction": str(trade.return_fraction),
                "holding_bars": trade.holding_bars,
                "exit_reason": trade.exit_reason.value,
            }
            for trade in result.closed_trades
        ],
    }


def qualify(manifest_path: Path, policy_path: Path) -> dict[str, object]:
    policy = load_policy(policy_path)
    manifested = ManifestedCsvHistoricalBarSource(
        manifest_path,
        policy=HistoricalDataPolicy(
            minimum_bars=60,
            maximum_jump_fraction=Decimal("0.25"),
        ),
    ).load()
    opening_cash = _decimal(policy, "opening_cash")
    target_quantity = _decimal(policy, "target_quantity")
    backtest = BacktestConfig(
        opening_cash=opening_cash,
        fee_per_fill=_decimal(policy, "fee_per_fill"),
        slippage_bps=_decimal(policy, "slippage_bps"),
    )
    signal = _object(policy, "signal")
    candidate = RegimeAwareMomentumStrategy(
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
    position = _object(policy, "position_management")
    position_policy = PositionManagementPolicy(
        stop_loss_fraction=_decimal(position, "stop_loss_fraction"),
        take_profit_fraction=_decimal(position, "take_profit_fraction"),
        trailing_activation_fraction=_decimal(position, "trailing_activation_fraction"),
        trailing_stop_fraction=_decimal(position, "trailing_stop_fraction"),
        maximum_holding_bars=_positive_int(position, "maximum_holding_bars"),
    )
    control = LongOnlyMomentumStrategy(
        strategy_id=str(policy["control_strategy_id"]),
        target_quantity=target_quantity,
    )
    training_bars = _positive_int(policy, "training_bars")

    regime_evidence: list[dict[str, object]] = []
    candidate_returns: list[Decimal] = []
    control_returns: list[Decimal] = []
    candidate_drawdowns: list[Decimal] = []
    control_drawdowns: list[Decimal] = []
    aggregate_trades = []

    for window in manifested.manifest.windows:
        bars = [
            bar
            for bar in manifested.dataset.bars
            if window.start <= bar.timestamp < window.end
        ]
        if len(bars) <= training_bars:
            raise ValueError(f"shadow regime lacks holdout bars:{window.name}")
        candidate_result = ManagedHistoricalBacktester(
            strategy=candidate,
            position_policy=position_policy,
            config=backtest,
        ).run(bars, first_execution_index=training_bars)
        control_result = HistoricalBacktester(
            strategy=control,
            config=backtest,
        ).run(bars, first_execution_index=training_bars)
        candidate_drawdown = candidate_result.max_drawdown / opening_cash
        control_drawdown = control_result.max_drawdown / opening_cash
        candidate_returns.append(candidate_result.total_return)
        control_returns.append(control_result.total_return)
        candidate_drawdowns.append(candidate_drawdown)
        control_drawdowns.append(control_drawdown)
        aggregate_trades.extend(candidate_result.closed_trades)
        regime_evidence.append(
            {
                "name": window.name,
                "bars": len(bars),
                "training_bars": training_bars,
                "holdout_bars": len(bars) - training_bars,
                "candidate": {
                    "fill_count": candidate_result.fill_count,
                    "ending_equity": str(candidate_result.ending_equity),
                    "total_pnl": str(candidate_result.total_pnl),
                    "total_return": str(candidate_result.total_return),
                    "max_drawdown_fraction": str(candidate_drawdown),
                    "final_quantity": str(candidate_result.final_quantity),
                    **_closed_trade_evidence(candidate_result),
                },
                "control": {
                    "fill_count": control_result.trades,
                    "ending_equity": str(control_result.ending_equity),
                    "total_pnl": str(control_result.total_pnl),
                    "total_return": str(control_result.total_return),
                    "max_drawdown_fraction": str(control_drawdown),
                    "final_quantity": str(control_result.final_quantity),
                },
                "comparison": {
                    "return_delta": str(
                        candidate_result.total_return - control_result.total_return
                    ),
                    "drawdown_delta": str(candidate_drawdown - control_drawdown),
                    "candidate_return_not_worse": (
                        candidate_result.total_return >= control_result.total_return
                    ),
                    "candidate_drawdown_not_worse": (
                        candidate_drawdown <= control_drawdown
                    ),
                },
            }
        )

    regime_count = Decimal(len(regime_evidence))
    mean_candidate_return = sum(candidate_returns, Decimal("0")) / regime_count
    mean_control_return = sum(control_returns, Decimal("0")) / regime_count
    wins = sum(trade.net_pnl > 0 for trade in aggregate_trades)
    losses = sum(trade.net_pnl < 0 for trade in aggregate_trades)
    breakeven = len(aggregate_trades) - wins - losses
    gross_profit = sum(
        (trade.net_pnl for trade in aggregate_trades if trade.net_pnl > 0),
        Decimal("0"),
    )
    gross_loss = sum(
        (trade.net_pnl for trade in aggregate_trades if trade.net_pnl < 0),
        Decimal("0"),
    )
    aggregate_profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    closed_count = len(aggregate_trades)
    win_rate = Decimal(wins) / Decimal(closed_count) if closed_count else Decimal("0")

    blockers = [
        "SHADOW_ONLY_POLICY",
        "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE",
        "SINGLE_SYMBOL_HISTORICAL_EVIDENCE",
        "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE",
    ]
    if aggregate_profit_factor is None or aggregate_profit_factor < Decimal("1"):
        blockers.append("AGGREGATE_PROFIT_FACTOR_BELOW_ONE")
    if gross_profit + gross_loss <= 0:
        blockers.append("AGGREGATE_CLOSED_TRADE_PNL_NOT_POSITIVE")

    return {
        "schema_version": _SCHEMA,
        "shadow_only": True,
        "promotion_allowed": False,
        "promotion_blockers": blockers,
        "dataset_id": manifested.dataset.dataset_id,
        "dataset_sha256": manifested.dataset.canonical_sha256,
        "manifest_sha256": manifested.manifest_sha256,
        "source_classification": manifested.manifest.source_classification,
        "strategy_id": candidate.strategy_id,
        "control_strategy_id": control.strategy_id,
        "acceptance_policy": policy,
        "aggregate": {
            "regime_count": len(regime_evidence),
            "mean_candidate_return": str(mean_candidate_return),
            "mean_control_return": str(mean_control_return),
            "mean_return_delta": str(mean_candidate_return - mean_control_return),
            "worst_candidate_drawdown_fraction": str(max(candidate_drawdowns)),
            "worst_control_drawdown_fraction": str(max(control_drawdowns)),
            "closed_trade_count": closed_count,
            "winning_trades": wins,
            "losing_trades": losses,
            "breakeven_trades": breakeven,
            "win_rate": str(win_rate),
            "gross_profit": str(gross_profit),
            "gross_loss": str(gross_loss),
            "net_closed_trade_pnl": str(gross_profit + gross_loss),
            "profit_factor": _serialize_decimal(aggregate_profit_factor),
            "candidate_mean_return_not_worse": (
                mean_candidate_return >= mean_control_return
            ),
            "candidate_worst_drawdown_not_worse": (
                max(candidate_drawdowns) <= max(control_drawdowns)
            ),
        },
        "regimes": regime_evidence,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate non-promotable strategy-quality shadow evidence"
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
