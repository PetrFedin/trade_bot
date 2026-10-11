"""Research-only entry quality diagnostics on the unchanged shipped portfolio.

Features use ONLY the completed bar preceding entry. Execution-bar high/low
are post-entry diagnostics, never available to the selector at decision time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_trade_attribution import run_shipped_portfolio


def _bucket(value: Decimal, low: Decimal, high: Decimal) -> str:
    return "LOW" if value < low else "HIGH" if value >= high else "MID"


def run_entry_forensics(
    *,
    bars_dir: Path,
    symbols: tuple[str, ...],
    fee_bps_per_fill: Decimal = Decimal("8"),
    slippage_bps: Decimal = Decimal("5"),
) -> dict:
    result, synchronized, timeline, paths = run_shipped_portfolio(
        bars_dir=bars_dir, symbols=symbols,
        fee_bps_per_fill=fee_bps_per_fill, slippage_bps=slippage_bps,
    )
    lookup = {symbol: {bar.timestamp: i for i, bar in enumerate(bars)}
              for symbol, bars in synchronized.items()}
    grouped: dict[str, list] = defaultdict(list)
    early: dict[str, list] = defaultdict(list)
    trades = result.closed_trades
    rows: list[dict] = []
    for trade in trades:
        bars = synchronized[trade.symbol]
        index = lookup[trade.symbol][trade.entry_time]
        # Some test fixtures and legitimate early-backtest entries have fewer
        # than 20 completed bars. Keep ALL trades in the sample but do not
        # mislabel a partial rolling window as a full 20-bar observation.
        if index < 1:
            raise ValueError("trade entry has no completed prior bar")
        prior = bars[index - 1]
        complete_history = index >= 20
        history = bars[index - 20:index] if complete_history else ()
        if any(bar.timestamp >= trade.entry_time for bar in history):
            raise ValueError("entry features include non-completed bars")
        prior_volatility = (
            sum((bar.high - bar.low) / bar.close for bar in history)
            / Decimal(len(history))
            if complete_history else None
        )
        momentum = (
            prior.close / bars[index - 20].close - Decimal("1")
            if complete_history else None
        )
        gap = bars[index].open / prior.close - Decimal("1")
        entry_bar = bars[index]
        entry_range = (entry_bar.high - entry_bar.low) / entry_bar.open
        volatility_bucket = (
            _bucket(prior_volatility, Decimal("0.025"), Decimal("0.055"))
            if prior_volatility is not None else "INSUFFICIENT_HISTORY"
        )
        trend_bucket = (
            "UP" if momentum > Decimal("0.03")
            else "DOWN" if momentum < Decimal("-0.03") else "FLAT"
        ) if momentum is not None else "INSUFFICIENT_HISTORY"
        early_exit = trade.holding_bars <= 1
        same_bar_exit = trade.holding_bars == 0
        group_id = f"{volatility_bucket}:{trend_bucket}"
        grouped[group_id].append(trade)
        if early_exit:
            early[group_id].append(trade)
        rows.append({
            "symbol": trade.symbol,
            "entry_time": trade.entry_time.isoformat(),
            "exit_time": trade.exit_time.isoformat(),
            "exit_reason": trade.exit_reason.value,
            "holding_bars": trade.holding_bars,
            "same_bar_exit": same_bar_exit,
            "within_one_bar_exit": early_exit,
            "completed_bars_before_entry": index,
            "prior_20_bar_features_available": complete_history,
            "prior_20_bar_mean_range_fraction": (
                str(prior_volatility) if prior_volatility is not None else None
            ),
            "prior_20_bar_momentum": (
                str(momentum) if momentum is not None else None
            ),
            "entry_open_gap_vs_prior_close": str(gap),
            "entry_bar_range_fraction_post_entry": str(entry_range),
            "prior_volatility_regime": volatility_bucket,
            "prior_trend_regime": trend_bucket,
            "net_pnl_before_funding": str(trade.net_pnl),
            "ambiguous_intrabar_exit": trade.ambiguous_intrabar_exit,
            "gap_through_stop": trade.gap_through_stop,
        })
    summary: dict[str, dict] = {}
    for key in sorted(grouped):
        subset = grouped[key]
        pnl = sum((t.net_pnl for t in subset), Decimal("0"))
        summary[key] = {
            "closed_trades": len(subset),
            "early_exits": len(early[key]),
            "same_bar_exits": sum(t.holding_bars == 0 for t in subset),
            "net_closed_pnl_before_funding": str(pnl),
            "early_exit_fraction": str(Decimal(len(early[key])) / Decimal(len(subset))),
        }
    source_sha = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in paths}
    payload = {
        "schema_version": "astra-entry-regime-forensics-research-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_DIAGNOSTIC_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "strategy_changed": False,
        "starting_cash": "10000",
        "source_sha256": dict(sorted(source_sha.items())),
        "symbols": list(symbols),
        "start": timeline[0].isoformat(),
        "end": timeline[-1].isoformat(),
        "cost_policy": {
            "fee_bps_per_fill": str(fee_bps_per_fill),
            "slippage_bps_per_side": str(slippage_bps),
        },
        "summary": {
            "trades": len(trades),
            "trades_with_insufficient_prior_history": sum(
                not row["prior_20_bar_features_available"] for row in rows
            ),
            "same_bar_exits": sum(t.holding_bars == 0 for t in trades),
            "next_bar_exits": sum(t.holding_bars == 1 for t in trades),
            "early_exits": sum(t.holding_bars <= 1 for t in trades),
            "closed_trade_net_pnl": str(sum((t.net_pnl for t in trades), Decimal("0"))),
            "portfolio_pnl_including_open_positions": str(result.total_pnl),
            "by_prior_regime": summary,
        },
        "trades": rows,
        "limitations": [
            "Volatility ranges are fixed descriptive bins, not fitted signal thresholds",
            "Early trades with fewer than 20 completed bars are retained but have null features",
            "Price-based realized range is a proxy, not order-book spread or ATR",
            "Execution-bar range is post-entry and forbidden as predictor",
            "Closed-trade PnL excludes unclosed positions; funding not allocated",
            "Daily OHLC cannot reconstruct stop/target intrabar ordering",
            "No candidate strategy or untouched holdout has been tested",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bars-dir", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    payload = run_entry_forensics(bars_dir=args.bars_dir, symbols=tuple(sorted(args.symbols)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
