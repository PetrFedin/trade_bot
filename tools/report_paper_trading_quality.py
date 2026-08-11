from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.application.paper_execution_quality import SQLitePaperExecutionQualityStore
from app.application.paper_quality_reporting import build_paper_trading_quality_report
from app.application.paper_trade_quality import (
    PaperTradeQualityTracker,
    SQLitePaperTradeQualityStore,
)
from app.strategy.quality_monitor import TradeQualityMonitorPolicy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit a JSON snapshot of exact paper trade and execution quality."
    )
    parser.add_argument("--trade-quality-db", required=True)
    parser.add_argument("--execution-quality-db")
    parser.add_argument(
        "--strategy-id",
        default="cross-sectional-quality-v2-paper-shadow",
    )
    parser.add_argument("--output")
    parser.add_argument("--window-trades", type=int, default=20)
    parser.add_argument("--minimum-observations", type=int, default=10)
    parser.add_argument("--minimum-profit-factor", default="1.0")
    parser.add_argument("--minimum-profit-preservation-rate", default="0.50")
    parser.add_argument("--minimum-average-mfe-capture-ratio", default="0.10")
    parser.add_argument("--maximum-hard-stop-fraction", default="0.50")
    parser.add_argument("--maximum-consecutive-losses", type=int, default=4)
    parser.add_argument(
        "--allow-entries-when-insufficient-data",
        action="store_true",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    policy = TradeQualityMonitorPolicy(
        window_trades=args.window_trades,
        minimum_observations=args.minimum_observations,
        minimum_profit_factor=Decimal(args.minimum_profit_factor),
        minimum_profit_preservation_rate=Decimal(
            args.minimum_profit_preservation_rate
        ),
        minimum_average_mfe_capture_ratio=Decimal(
            args.minimum_average_mfe_capture_ratio
        ),
        maximum_hard_stop_fraction=Decimal(args.maximum_hard_stop_fraction),
        maximum_consecutive_losses=args.maximum_consecutive_losses,
        allow_entries_when_insufficient_data=args.allow_entries_when_insufficient_data,
    )
    tracker = PaperTradeQualityTracker(
        store=SQLitePaperTradeQualityStore(args.trade_quality_db),
        strategy_id=args.strategy_id,
    )
    execution_store = (
        None
        if args.execution_quality_db is None
        else SQLitePaperExecutionQualityStore(args.execution_quality_db)
    )
    report = build_paper_trading_quality_report(
        tracker=tracker,
        policy=policy,
        generated_at=datetime.now(UTC),
        execution_store=execution_store,
    )
    rendered = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    Path(args.output).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
