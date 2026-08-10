from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.historical import HistoricalDataPolicy
from app.marketdata.manifest import ManifestedCsvHistoricalBarSource
from app.strategy.backtest import BacktestConfig
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.qualification import WalkForwardPolicy, WalkForwardQualifier
from app.strategy.regimes import HistoricalRegime, MultiRegimeQualifier


def _decimal(data: dict[str, Any], field: str) -> Decimal:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != "strategy-qualification-v1":
        raise ValueError("strategy qualification policy schema mismatch")
    return data


def qualify(manifest_path: Path, policy_path: Path) -> dict[str, object]:
    policy_data = load_policy(policy_path)
    manifested = ManifestedCsvHistoricalBarSource(
        manifest_path,
        policy=HistoricalDataPolicy(
            minimum_bars=60,
            maximum_jump_fraction=Decimal("0.25"),
        ),
    ).load()
    dataset = manifested.dataset
    strategy = LongOnlyMomentumStrategy(
        strategy_id=str(policy_data["strategy_id"]),
        target_quantity=_decimal(policy_data, "target_quantity"),
    )
    backtest = BacktestConfig(
        opening_cash=_decimal(policy_data, "opening_cash"),
        fee_per_fill=_decimal(policy_data, "fee_per_fill"),
        slippage_bps=_decimal(policy_data, "slippage_bps"),
    )
    walk = policy_data.get("walk_forward")
    if not isinstance(walk, dict):
        raise ValueError("walk_forward policy is required")
    qualifier = WalkForwardQualifier(
        strategy=strategy,
        backtest_config=backtest,
        policy=WalkForwardPolicy(
            training_bars=int(walk["training_bars"]),
            testing_bars=int(walk["testing_bars"]),
            step_bars=int(walk["step_bars"]),
            minimum_windows=int(walk["minimum_windows"]),
            maximum_drawdown_fraction=_decimal(walk, "maximum_drawdown_fraction"),
            minimum_mean_oos_return=_decimal(walk, "minimum_mean_oos_return"),
            minimum_mean_excess_return=_decimal(walk, "minimum_mean_excess_return"),
            require_trade_in_each_window=bool(walk["require_trade_in_each_window"]),
        ),
    )
    regimes = tuple(
        HistoricalRegime(window.name, window.start, window.end)
        for window in manifested.manifest.windows
    )
    result = MultiRegimeQualifier(
        qualifier=qualifier,
        minimum_regimes=len(regimes),
    ).qualify(dataset, regimes)
    evidence = {
        "qualified": result.qualified,
        "reasons": list(result.reasons),
        "dataset_id": result.dataset_id,
        "dataset_sha256": result.dataset_sha256,
        "manifest_sha256": manifested.manifest_sha256,
        "source_classification": manifested.manifest.source_classification,
        "upstream_repository": manifested.manifest.upstream_repository,
        "upstream_path": manifested.manifest.upstream_path,
        "upstream_git_blob_sha": manifested.manifest.upstream_git_blob_sha,
        "strategy_id": strategy.strategy_id,
        "target_quantity": str(strategy.target_quantity),
        "acceptance_policy": policy_data,
        "regimes": [
            {
                "name": item.regime.name,
                "bars": item.bars,
                "qualified": item.qualification.qualified,
                "reasons": list(item.qualification.reasons),
                "windows": len(item.qualification.windows),
                "mean_oos_return": str(item.qualification.mean_oos_return),
                "mean_excess_return": str(item.qualification.mean_excess_return),
                "worst_drawdown_fraction": str(item.qualification.worst_drawdown_fraction),
                "total_trades": item.qualification.total_trades,
            }
            for item in result.results
        ],
    }
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify a manifested historical strategy snapshot"
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
    return 0 if evidence["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
