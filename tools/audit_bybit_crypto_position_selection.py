from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    rank_crypto_signals,
)
from app.strategy.crypto_position_selection import (
    CryptoPositionCandidate,
    average_expected_net_r,
    select_crypto_positions,
)
from tools.replay_bybit_crypto import default_crypto_config

_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "ADAUSDT",
)


def audit_position_selection(
    acquisition: BybitKlineAcquisition,
    *,
    equity_usdt: Decimal = Decimal("1000"),
    config: CryptoPerpStrategyConfig | None = None,
    maximum_positions: int = 2,
    maximum_examples: int = 50,
) -> dict[str, Any]:
    """Compare current signal-quality ordering with a cost-aware plan ranking."""

    if equity_usdt <= 0:
        raise ValueError("selection audit equity must be positive")
    if maximum_positions < 1:
        raise ValueError("selection audit maximum_positions must be positive")
    if maximum_examples < 0:
        raise ValueError("selection audit maximum_examples cannot be negative")
    active = default_crypto_config() if config is None else config
    active = active.with_target(Decimal("20"))
    active.validate()

    grouped = _bars_by_symbol_time(acquisition.bars)
    common_times = _common_times(grouped)
    histories: dict[str, list[BybitKlineBar]] = {
        symbol: [] for symbol in sorted(grouped)
    }
    decision_count = 0
    comparable_decision_count = 0
    divergent_set_count = 0
    divergent_order_count = 0
    baseline_expected_r_total = Decimal("0")
    economic_expected_r_total = Decimal("0")
    baseline_expected_edge_total = Decimal("0")
    economic_expected_edge_total = Decimal("0")
    examples: list[dict[str, Any]] = []

    for timestamp in common_times:
        for symbol, rows in grouped.items():
            histories[symbol].append(rows[timestamp])
        rankings = rank_crypto_signals(histories, active)
        current_order: list[CryptoPositionCandidate] = []
        for evaluation in rankings:
            if evaluation.signal is None:
                continue
            plan_evaluation = build_trade_plan(
                evaluation.signal,
                equity_usdt=equity_usdt,
                config=active,
            )
            if not plan_evaluation.eligible or plan_evaluation.plan is None:
                continue
            current_order.append(
                CryptoPositionCandidate(
                    signal=evaluation.signal,
                    plan=plan_evaluation.plan,
                )
            )
        if not current_order:
            continue
        decision_count += 1
        current_selected = tuple(current_order[:maximum_positions])
        economic = select_crypto_positions(
            current_order,
            maximum_positions=maximum_positions,
        )
        economic_selected = economic.selected
        if len(current_order) < 2:
            continue
        comparable_decision_count += 1
        current_symbols = tuple(row.plan.symbol for row in current_selected)
        economic_symbols = tuple(row.plan.symbol for row in economic_selected)
        if set(current_symbols) != set(economic_symbols):
            divergent_set_count += 1
        if current_symbols != economic_symbols:
            divergent_order_count += 1

        current_r = average_expected_net_r(current_selected)
        economic_r = average_expected_net_r(economic_selected)
        if current_r is None or economic_r is None:
            raise ValueError("selection audit expected non-empty selections")
        baseline_expected_r_total += current_r
        economic_expected_r_total += economic_r
        baseline_edge = _average(
            row.plan.expected_net_edge_usd for row in current_selected
        )
        economic_edge = _average(
            row.plan.expected_net_edge_usd for row in economic_selected
        )
        baseline_expected_edge_total += baseline_edge
        economic_expected_edge_total += economic_edge

        if current_symbols != economic_symbols and len(examples) < maximum_examples:
            examples.append(
                {
                    "decision_time": timestamp.isoformat(),
                    "current_symbols": list(current_symbols),
                    "economic_symbols": list(economic_symbols),
                    "current_average_expected_net_r": float(current_r),
                    "economic_average_expected_net_r": float(economic_r),
                    "current_average_expected_net_edge_usd": float(baseline_edge),
                    "economic_average_expected_net_edge_usd": float(economic_edge),
                }
            )

    denominator = Decimal(comparable_decision_count) if comparable_decision_count else None
    return {
        "qualification": "BYBIT_CRYPTO_POSITION_SELECTION_AUDIT",
        "decision_count_with_eligible_plan": decision_count,
        "comparable_decision_count": comparable_decision_count,
        "top_set_divergence_count": divergent_set_count,
        "top_order_divergence_count": divergent_order_count,
        "top_set_divergence_fraction": (
            None
            if denominator is None
            else float(Decimal(divergent_set_count) / denominator)
        ),
        "top_order_divergence_fraction": (
            None
            if denominator is None
            else float(Decimal(divergent_order_count) / denominator)
        ),
        "current_average_expected_net_r": (
            None
            if denominator is None
            else float(baseline_expected_r_total / denominator)
        ),
        "economic_average_expected_net_r": (
            None
            if denominator is None
            else float(economic_expected_r_total / denominator)
        ),
        "expected_net_r_uplift": (
            None
            if denominator is None
            else float(
                (economic_expected_r_total - baseline_expected_r_total)
                / denominator
            )
        ),
        "current_average_expected_net_edge_usd": (
            None
            if denominator is None
            else float(baseline_expected_edge_total / denominator)
        ),
        "economic_average_expected_net_edge_usd": (
            None
            if denominator is None
            else float(economic_expected_edge_total / denominator)
        ),
        "expected_net_edge_uplift_usd": (
            None
            if denominator is None
            else float(
                (economic_expected_edge_total - baseline_expected_edge_total)
                / denominator
            )
        ),
        "divergence_examples": examples,
        "current_ranking_contract": "signal_quality_score_desc_then_existing_tie_break",
        "economic_ranking_contract": [
            "expected_net_r_desc",
            "expected_net_edge_usd_desc",
            "quality_score_desc",
            "cost_to_target_fraction_asc",
            "symbol_asc",
        ],
        "equity_assumption_usdt": float(equity_usdt),
        "target_net_profit_usd": 20.0,
        "realized_pnl_compared": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
    }


def acquire_archive_and_audit_position_selection(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 14,
    equity_usdt: Decimal = Decimal("1000"),
    client: BybitPublicTradeArchiveClient | None = None,
) -> dict[str, Any]:
    if lookback_days < 1:
        raise ValueError("selection audit lookback_days must be positive")
    dates = completed_archive_dates(lookback_days=lookback_days)
    archive = BybitPublicTradeArchiveClient() if client is None else client
    acquisition = archive.fetch_klines(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = audit_position_selection(
        acquisition.klines,
        equity_usdt=equity_usdt,
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        archive_dates=[value.isoformat() for value in dates],
        symbols=list(symbols),
        archive_completed_utc_days_only=True,
        raw_trade_archive_committed_to_repository=False,
    )
    return report


def _bars_by_symbol_time(
    bars: tuple[BybitKlineBar, ...],
) -> dict[str, dict[datetime, BybitKlineBar]]:
    grouped: dict[str, dict[datetime, BybitKlineBar]] = defaultdict(dict)
    for bar in bars:
        if bar.start_time in grouped[bar.symbol]:
            raise ValueError("selection audit duplicate symbol timestamp")
        grouped[bar.symbol][bar.start_time] = bar
    return dict(grouped)


def _common_times(
    grouped: dict[str, dict[datetime, BybitKlineBar]],
) -> tuple[datetime, ...]:
    if not grouped:
        return ()
    sets = [set(rows) for rows in grouped.values()]
    return tuple(sorted(set.intersection(*sets)))


def _average(values: Any) -> Decimal:
    rows = tuple(values)
    if not rows:
        raise ValueError("selection audit average requires values")
    return sum(rows, start=Decimal("0")) / Decimal(len(rows))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Bybit crypto position ranking")
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--equity", default="1000")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    report = acquire_archive_and_audit_position_selection(
        symbols=symbols,
        lookback_days=args.lookback_days,
        equity_usdt=Decimal(args.equity),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_POSITION_SELECTION_AUDIT=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
