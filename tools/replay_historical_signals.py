from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioResult,
)
from tools import qualify_cross_sectional_trading_quality_shadow as quality
from tools import qualify_entry_quality_shadow as entry_quality
from tools import qualify_profit_runner_shadow as profit_runner
from tools import qualify_selection_exit_confirmation_shadow as selection_exit

_SCHEMA = "historical-signal-replay-evidence-v1"


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _bar_fingerprint(bars: tuple[OhlcvBar, ...]) -> str:
    payload = "\n".join(
        "|".join(
            (
                bar.symbol,
                bar.timestamp.isoformat(),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume),
                str(bar.trade_count),
                "" if bar.vwap is None else str(bar.vwap),
            )
        )
        for bar in bars
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trade_row(variant: str, trade) -> dict[str, object]:
    return {
        "variant": variant,
        "symbol": trade.symbol,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_execution_price": str(trade.entry_execution_price),
        "exit_execution_price": str(trade.exit_execution_price),
        "quantity": str(trade.quantity),
        "net_pnl": str(trade.net_pnl),
        "holding_bars": trade.holding_bars,
        "exit_reason": trade.exit_reason.value,
        "maximum_favorable_excursion_fraction": str(
            trade.maximum_favorable_excursion_fraction
        ),
        "maximum_adverse_excursion_fraction": str(
            trade.maximum_adverse_excursion_fraction
        ),
        "mfe_capture_ratio": _decimal(trade.mfe_capture_ratio),
        "mfe_giveback_fraction": _decimal(trade.mfe_giveback_fraction),
        "ambiguous_intrabar_exit": trade.ambiguous_intrabar_exit,
        "gap_through_stop": trade.gap_through_stop,
    }


def _decision_row(variant: str, trace) -> dict[str, object]:
    return {
        "variant": variant,
        "execution_index": trace.execution_index,
        "decision_time": trace.decision_time.isoformat(),
        "execution_time": trace.execution_time.isoformat(),
        "selected_symbols": list(trace.selected_symbols),
        "entered_symbols": list(trace.entered_symbols),
        "open_exit_symbols": list(trace.open_exit_symbols),
        "intrabar_exit_symbols": list(trace.intrabar_exit_symbols),
        "pending_selection_exit_symbols": list(trace.pending_selection_exit_symbols),
        "blocked_entries": [
            {"symbol": symbol, "reason": reason.value}
            for symbol, reason in trace.blocked_entries
        ],
        "equity_at_prior_close": str(trace.equity_at_prior_close),
        "closing_equity": str(trace.closing_equity),
        "closing_gross_exposure_fraction": str(
            trace.closing_gross_exposure_fraction
        ),
        "concurrent_positions": trace.concurrent_positions,
    }


def _variant_evidence(
    name: str,
    result: CrossSectionalPortfolioResult,
    *,
    timeline: set[datetime],
) -> dict[str, object]:
    reason_counts = Counter(trade.exit_reason.value for trade in result.closed_trades)
    no_lookahead = all(
        trace.decision_time < trace.execution_time
        and trace.decision_time in timeline
        and trace.execution_time in timeline
        for trace in result.decision_trace
    )
    trade_timestamps_valid = all(
        trade.entry_time in timeline
        and trade.exit_time in timeline
        and trade.entry_time <= trade.exit_time
        for trade in result.closed_trades
    )
    if not no_lookahead or not trade_timestamps_valid:
        raise RuntimeError(f"historical replay timestamp invariant failed:{name}")
    metrics = dict(quality._result_metrics(result))
    metrics["exit_reason_counts"] = dict(sorted(reason_counts.items()))
    metrics["one_bar_reentry_count"] = result.one_bar_reentry_count
    metrics["selection_exit_confirmation_pending_count"] = (
        result.selection_exit_confirmation_pending_count
    )
    return {
        "metrics": metrics,
        "closed_trades": [_trade_row(name, trade) for trade in result.closed_trades],
        "decision_trace": [_decision_row(name, trace) for trace in result.decision_trace],
        "no_lookahead_verified": True,
        "trade_timestamps_verified": True,
    }


def _filter_completed_through(
    bars: tuple[OhlcvBar, ...], completed_through: date | None
) -> tuple[tuple[OhlcvBar, ...], int]:
    if completed_through is None:
        return bars, 0
    retained = tuple(bar for bar in bars if bar.timestamp.date() <= completed_through)
    excluded = len(bars) - len(retained)
    if not retained:
        raise ValueError("completed-through cutoff excludes all replay bars")
    return retained, excluded


def _run_entry_selection_interaction(
    bars: tuple[OhlcvBar, ...],
    *,
    config: dict,
    entry_policy: dict,
    selection_policy: dict,
) -> CrossSectionalPortfolioResult:
    return CrossSectionalPortfolioBacktester(
        selector=entry_quality._selector(
            config,
            filtered=True,
            policy=entry_policy,
        ),
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=quality._position_policy(config, protection=True),
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
        selection_exit_policy=selection_exit._selection_exit_policy(selection_policy),
    ).run(bars)


def replay(
    *,
    csv_path: Path,
    trading_quality_config_path: Path,
    entry_quality_policy_path: Path,
    selection_exit_policy_path: Path,
    profit_runner_policy_path: Path,
    completed_through: date | None = None,
    source_label: str = "CSV_INPUT_UNVERIFIED",
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    entry_policy = entry_quality.load_policy(entry_quality_policy_path)
    selection_policy = selection_exit.load_policy(selection_exit_policy_path)
    runner_policy = profit_runner.load_policy(profit_runner_policy_path)

    raw = quality.read_csv(csv_path)
    raw, excluded_by_cutoff = _filter_completed_through(raw, completed_through)
    bars = quality.synchronize_common_timestamps(
        raw,
        minimum_symbols=quality._positive_int(config, "minimum_symbols"),
    )
    timestamps = sorted({bar.timestamp for bar in bars})
    signal_config = quality._signal_config(config)
    if len(timestamps) <= signal_config.minimum_history_bars:
        raise ValueError("insufficient synchronized history for historical replay")

    baseline = entry_quality._run(
        bars,
        config=config,
        policy=entry_policy,
        filtered=False,
    )
    variants = {
        "CURRENT_COMBINED_SHADOW": baseline,
        "ENTRY_QUALITY_CANDIDATE": entry_quality._run(
            bars,
            config=config,
            policy=entry_policy,
            filtered=True,
        ),
        "SELECTION_EXIT_CONFIRMATION_CANDIDATE": selection_exit._run(
            bars,
            config=config,
            policy=selection_policy,
            confirmed=True,
        ),
        "ENTRY_QUALITY_PLUS_SELECTION_EXIT_INTERACTION": (
            _run_entry_selection_interaction(
                bars,
                config=config,
                entry_policy=entry_policy,
                selection_policy=selection_policy,
            )
        ),
        "PROFIT_RUNNER_CANDIDATE": profit_runner._run(
            bars,
            config=config,
            runner=True,
        ),
    }
    timeline = set(timestamps)
    evidence = {
        name: _variant_evidence(name, result, timeline=timeline)
        for name, result in variants.items()
    }
    baseline_metrics = evidence["CURRENT_COMBINED_SHADOW"]["metrics"]
    comparisons = {
        name: quality._comparison(payload["metrics"], baseline_metrics)
        for name, payload in evidence.items()
        if name != "CURRENT_COMBINED_SHADOW"
    }
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_HISTORICAL_REPLAY",
        "evidence_scope": "OBSERVED_MARKET_HISTORY_REPLAY",
        "source_label": source_label,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "replay_bars_sha256": _bar_fingerprint(bars),
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "paper_order_writes_enabled": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "real_paper_fills": False,
        "counterfactual_candidates_promoted": False,
        "completed_through": (
            None if completed_through is None else completed_through.isoformat()
        ),
        "bars_excluded_by_cutoff": excluded_by_cutoff,
        "symbols": sorted({bar.symbol for bar in bars}),
        "synchronized_timestamp_count": len(timestamps),
        "first_timestamp": timestamps[0].isoformat(),
        "last_timestamp": timestamps[-1].isoformat(),
        "cost_model": {
            "fee_per_fill": config["fee_per_fill"],
            "slippage_bps": config["slippage_bps"],
            "intrabar_path": "CONSERVATIVE_PROTECTIVE_STOP_PRIORITY",
            "selection_and_time_exit": "NEXT_OPEN",
            "entry": "NEXT_OPEN_AFTER_COMPLETED_SIGNAL_BAR",
        },
        "baseline_components": list(config["candidate_components"]),
        "candidate_policies": {
            "entry_quality": entry_policy["policy"],
            "selection_exit_confirmation": selection_policy["policy"],
            "profit_runner": runner_policy["policy"],
        },
        "declared_interaction_checks": [
            "ENTRY_QUALITY_PLUS_SELECTION_EXIT_INTERACTION"
        ],
        "variants": evidence,
        "comparisons_vs_current_combined_shadow": comparisons,
        "limitations": [
            "HISTORICAL_REPLAY_IS_NOT_LIVE_OR_REAL_PAPER_EXECUTION",
            "STATIC_SLIPPAGE_AND_FEE_MODEL_DOES_NOT_CAPTURE_ORDER_BOOK_IMPACT",
            "DAILY_BARS_DO_NOT_RECONSTRUCT_TRUE_INTRABAR_PATH",
            "CANDIDATES_REMAIN_NON_PROMOTED_DESPITE_MARGINAL_OR_PAIRWISE_RESULTS",
            "PAIRWISE_RESULT_DOES_NOT_ESTABLISH_FULL_STACK_INTERACTION_SAFETY",
            "SAME_SAMPLE_REPLAY_IS_NOT_STRATEGY_PROMOTION_EVIDENCE",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def _write_trade_csv(report: dict[str, object], path: Path) -> None:
    variants = report["variants"]
    rows = [
        trade
        for payload in variants.values()
        for trade in payload["closed_trades"]
    ]
    fields = [
        "variant",
        "symbol",
        "entry_time",
        "exit_time",
        "entry_execution_price",
        "exit_execution_price",
        "quantity",
        "net_pnl",
        "holding_bars",
        "exit_reason",
        "maximum_favorable_excursion_fraction",
        "maximum_adverse_excursion_fraction",
        "mfe_capture_ratio",
        "mfe_giveback_fraction",
        "ambiguous_intrabar_exit",
        "gap_through_stop",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_decision_csv(report: dict[str, object], path: Path) -> None:
    variants = report["variants"]
    rows = [
        decision
        for payload in variants.values()
        for decision in payload["decision_trace"]
    ]
    fields = [
        "variant",
        "execution_index",
        "decision_time",
        "execution_time",
        "selected_symbols",
        "entered_symbols",
        "open_exit_symbols",
        "intrabar_exit_symbols",
        "pending_selection_exit_symbols",
        "blocked_entries",
        "equity_at_prior_close",
        "closing_equity",
        "closing_gross_exposure_fraction",
        "concurrent_positions",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            serialized = dict(row)
            for field in (
                "selected_symbols",
                "entered_symbols",
                "open_exit_symbols",
                "intrabar_exit_symbols",
                "pending_selection_exit_symbols",
                "blocked_entries",
            ):
                serialized[field] = json.dumps(serialized[field], sort_keys=True)
            writer.writerow(serialized)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay completed historical bars through current shadow trading logic"
    )
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--entry-quality-policy", type=Path, required=True)
    parser.add_argument("--selection-exit-policy", type=Path, required=True)
    parser.add_argument("--profit-runner-policy", type=Path, required=True)
    parser.add_argument("--completed-through", type=date.fromisoformat)
    parser.add_argument("--source-label", default="CSV_INPUT_UNVERIFIED")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trades-output", type=Path)
    parser.add_argument("--decisions-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = replay(
        csv_path=args.csv,
        trading_quality_config_path=args.trading_quality_config,
        entry_quality_policy_path=args.entry_quality_policy,
        selection_exit_policy_path=args.selection_exit_policy,
        profit_runner_policy_path=args.profit_runner_policy,
        completed_through=args.completed_through,
        source_label=args.source_label,
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    if args.trades_output is not None:
        _write_trade_csv(report, args.trades_output)
    if args.decisions_output is not None:
        _write_decision_csv(report, args.decisions_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
