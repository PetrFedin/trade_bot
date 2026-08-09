from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.historical import CsvHistoricalBarSource, HistoricalDataPolicy
from app.strategy.backtest import BacktestConfig
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.qualification import WalkForwardPolicy, WalkForwardQualifier
from app.strategy.qualification_manifest import (
    DatasetProvenance,
    build_qualification_manifest,
    file_sha256,
    load_qualification_manifest,
    write_qualification_manifest,
)
from app.strategy.regimes import HistoricalRegime, MultiRegimeQualifier

SPEC_SCHEMA = "strategy-qualification-spec-v1"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("regime timestamps must be timezone-aware")
    return parsed


def _load_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SPEC_SCHEMA:
        raise ValueError(f"spec schema must be {SPEC_SCHEMA}")
    return payload


def _run(dataset_path: Path, spec_path: Path, output: Path) -> None:
    spec = _load_spec(spec_path)
    dataset_policy = spec["dataset_policy"]
    dataset = CsvHistoricalBarSource(
        dataset_path,
        policy=HistoricalDataPolicy(
            minimum_bars=int(dataset_policy["minimum_bars"]),
        ),
        expected_symbol=str(spec["expected_symbol"]),
    ).load()
    strategy_spec = spec["strategy"]
    strategy = LongOnlyMomentumStrategy(
        strategy_id=str(strategy_spec["strategy_id"]),
        target_quantity=Decimal(str(strategy_spec["target_quantity"])),
    )
    backtest_spec = spec["backtest"]
    backtest = BacktestConfig(
        opening_cash=Decimal(str(backtest_spec["opening_cash"])),
        fee_per_fill=Decimal(str(backtest_spec["fee_per_fill"])),
        slippage_bps=Decimal(str(backtest_spec["slippage_bps"])),
        minimum_history_bars=int(backtest_spec["minimum_history_bars"]),
    )
    policy_spec = spec["walk_forward_policy"]
    policy = WalkForwardPolicy(
        training_bars=int(policy_spec["training_bars"]),
        testing_bars=int(policy_spec["testing_bars"]),
        step_bars=int(policy_spec["step_bars"]),
        minimum_windows=int(policy_spec["minimum_windows"]),
        maximum_drawdown_fraction=Decimal(str(policy_spec["maximum_drawdown_fraction"])),
        minimum_mean_oos_return=Decimal(str(policy_spec["minimum_mean_oos_return"])),
        minimum_mean_excess_return=Decimal(str(policy_spec["minimum_mean_excess_return"])),
        require_trade_in_each_window=bool(policy_spec["require_trade_in_each_window"]),
    )
    walk_forward = WalkForwardQualifier(strategy=strategy, backtest_config=backtest, policy=policy)
    regimes = tuple(
        HistoricalRegime(
            name=str(item["name"]),
            start=_timestamp(str(item["start"])),
            end=_timestamp(str(item["end"])),
        )
        for item in spec["regimes"]
    )
    qualifier = MultiRegimeQualifier(
        qualifier=walk_forward,
        minimum_regimes=int(spec["minimum_regimes"]),
    )
    provenance_spec = spec["dataset_provenance"]
    provenance = DatasetProvenance(
        classification=str(provenance_spec["classification"]),
        provider=str(provenance_spec["provider"]),
        source_reference=str(provenance_spec["source_reference"]),
    )
    manifest = build_qualification_manifest(
        dataset=dataset,
        provenance=provenance,
        strategy=strategy,
        qualifier=qualifier,
        regimes=regimes,
        spec_sha256=file_sha256(spec_path),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_qualification_manifest(output, manifest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or verify strategy qualification manifests")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("manifest", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "run":
        _run(args.dataset, args.spec, args.output)
    else:
        load_qualification_manifest(args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
