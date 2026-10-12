"""Attribute unchanged Strategy A portfolio PnL to predetermined prior-bar BTC regimes.

Not an optimizer or counterfactual strategy. The regime label at the execution
day is formed ONLY from BTC completed bars preceding that day. Every marked
daily portfolio PnL delta (including unrealized final positions) is assigned
exactly once. Inputs are previously inspected historical Bybit daily bars,
so this is a diagnostic, NOT independent out-of-sample profitability evidence.
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

TREND_LOOKBACK_RETURNS = 30
VOL_LOOKBACK_RETURNS = 20
BULL_RETURN = Decimal("0.10")
BEAR_RETURN = Decimal("-0.10")
HIGH_MEAN_ABS_DAILY_RETURN = Decimal("0.015")
D = Decimal
BASE_CAPITAL = Decimal("10000")
FEE_PER_FILL_BPS = Decimal("8")
SLIPPAGE_BPS = Decimal("5")


def prior_completed_btc_regime(bars: tuple, execution_index: int) -> tuple[str, str]:
    """Use BTC close[t-1], close[t-31] and returns ending at t-1; never t."""
    if execution_index < TREND_LOOKBACK_RETURNS + 1:
        return "INSUFFICIENT_PRIOR_CONTEXT", "INSUFFICIENT_PRIOR_CONTEXT"
    if execution_index >= len(bars):
        raise ValueError("execution index outside BTC history")
    old = bars[execution_index - TREND_LOOKBACK_RETURNS - 1].close
    last = bars[execution_index - 1].close
    if old <= 0 or last <= 0:
        raise ValueError("regime source contains nonpositive prior price")
    trend_return = last / old - D("1")
    trend = "BULL" if trend_return >= BULL_RETURN else (
        "BEAR" if trend_return <= BEAR_RETURN else "SIDEWAYS"
    )
    deviations = []
    for i in range(execution_index - VOL_LOOKBACK_RETURNS, execution_index):
        before = bars[i - 1].close
        after = bars[i].close
        if before <= 0 or after <= 0:
            raise ValueError("regime volatility inputs must be positive")
        deviations.append(abs(after / before - D("1")))
    mean_absolute_daily_return = sum(deviations, D("0")) / D(len(deviations))
    volatility = (
        "HIGH_VOL" if mean_absolute_daily_return >= HIGH_MEAN_ABS_DAILY_RETURN
        else "LOW_VOL"
    )
    return trend, volatility


def _summarize(rows: list[dict], *, opening_cash: Decimal) -> dict:
    pnl = sum((row["pnl"] for row in rows), D("0"))
    closed_net = sum((row["closed_trade_net"] for row in rows), D("0"))
    exposure = sum((row["gross_exposure"] for row in rows), D("0"))
    return {
        "days": len(rows),
        "net_portfolio_pnl_usd_before_funding": str(pnl),
        "contribution_pct_of_starting_capital": str(pnl / opening_cash * D("100")),
        "closed_trade_pnl_usd_before_funding": str(closed_net),
        "closed_trades": sum(row["closed_trades"] for row in rows),
        "hard_stop_closed_trades": sum(row["hard_stop_exits"] for row in rows),
        "ambiguous_closed_trades": sum(row["ambiguous_exits"] for row in rows),
        "entries": sum(row["entries"] for row in rows),
        "exits": sum(row["exits"] for row in rows),
        "positive_days": sum(row["pnl"] > 0 for row in rows),
        "negative_days": sum(row["pnl"] < 0 for row in rows),
        "flat_days": sum(row["pnl"] == 0 for row in rows),
        "average_end_of_day_exposure_fraction": (
            str(exposure / D(len(rows))) if rows else None
        ),
        "worst_single_day_pnl_usd": str(min(row["pnl"] for row in rows)) if rows else None,
        "best_single_day_pnl_usd": str(max(row["pnl"] for row in rows)) if rows else None,
    }


def run_market_regime_attribution(
    *,
    bars_dir: Path,
    symbols: tuple[str, ...],
    opening_cash: Decimal = BASE_CAPITAL,
    fee_bps_per_fill: Decimal = FEE_PER_FILL_BPS,
    slippage_bps: Decimal = SLIPPAGE_BPS,
) -> dict:
    if "BTCUSDT" not in symbols:
        raise ValueError("BTCUSDT must be in universe as exogenous regime anchor")
    if opening_cash != D("10000"):
        raise ValueError("frozen Strategy A capital must stay 10000 USD")
    result, synchronized, timeline, paths = run_shipped_portfolio(
        bars_dir=bars_dir,
        symbols=symbols,
        opening_cash=opening_cash,
        fee_bps_per_fill=fee_bps_per_fill,
        slippage_bps=slippage_bps,
    )
    btc = synchronized["BTCUSDT"]
    if tuple(bar.timestamp for bar in btc) != timeline:
        raise ValueError("BTC regime clock differs from synchronized portfolio")
    by_exit_time: dict = defaultdict(list)
    for trade in result.closed_trades:
        by_exit_time[trade.exit_time].append(trade)

    bucket_rows: dict[str, list] = defaultdict(list)
    btc_trend_rows: dict[str, list] = defaultdict(list)
    btc_vol_rows: dict[str, list] = defaultdict(list)
    previous_equity = result.opening_cash
    rows = []
    for trace in result.decision_trace:
        index = trace.execution_index
        if index <= 0 or index >= len(timeline):
            raise ValueError("invalid decision execution index")
        if trace.execution_time != timeline[index]:
            raise ValueError("mismatched execution timestamp")
        trend, volatility = prior_completed_btc_regime(btc, index)
        trades = by_exit_time.pop(trace.execution_time, [])
        pnl = trace.closing_equity - previous_equity
        previous_equity = trace.closing_equity
        row = {
            "date": trace.execution_time.isoformat(),
            "trend": trend,
            "volatility": volatility,
            "group": f"{trend}:{volatility}",
            "pnl": pnl,
            "closed_trade_net": sum((t.net_pnl for t in trades), D("0")),
            "closed_trades": len(trades),
            "hard_stop_exits": sum(
                t.exit_reason.value == "INTRABAR_HARD_STOP" for t in trades
            ),
            "ambiguous_exits": sum(t.ambiguous_intrabar_exit for t in trades),
            "entries": len(trace.entered_symbols),
            "exits": len(trace.open_exit_symbols) + len(trace.intrabar_exit_symbols),
            "gross_exposure": trace.closing_gross_exposure_fraction,
        }
        rows.append(row)
        bucket_rows[row["group"]].append(row)
        btc_trend_rows[trend].append(row)
        btc_vol_rows[volatility].append(row)

    if by_exit_time:
        raise ValueError("closed trade with no corresponding portfolio decision day")
    if not rows:
        raise ValueError("strategy has no portfolio decision trace")
    if previous_equity != result.ending_equity:
        raise ValueError("final marked daily equity disagrees with portfolio")
    total_pnl = sum((row["pnl"] for row in rows), D("0"))
    if abs(total_pnl - result.total_pnl) > D("0.000001"):
        raise ValueError("daily regime PnL does not conserve portfolio PnL")
    if sum(row["closed_trades"] for row in rows) != result.closed_trade_count:
        raise ValueError("daily regime trade counts are not conserved")
    if sum(len(group) for group in bucket_rows.values()) != len(rows):
        raise ValueError("regime counts are not conserved")

    payload: dict[str, object] = {
        "schema_version": "astra-prior-completed-btc-regime-attribution-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_REGIME_DIAGNOSTIC_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "untouched_out_of_sample": False,
        "strategy_parameters_changed": False,
        "funding_allocated_per_day": False,
        "modelled_fee_bps_per_fill": str(fee_bps_per_fill),
        "modelled_slippage_bps_per_side": str(slippage_bps),
        "symbols": list(symbols),
        "data_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)
        },
        "definition": {
            "anchor": "BTCUSDT prior completed daily close only",
            "prior_close_to_close_trend_returns": TREND_LOOKBACK_RETURNS,
            "bull_if_prior_return_gte": str(BULL_RETURN),
            "bear_if_prior_return_lte": str(BEAR_RETURN),
            "sideways_otherwise": True,
            "mean_abs_prior_daily_return_window": VOL_LOOKBACK_RETURNS,
            "high_vol_if_mean_abs_daily_return_gte": str(HIGH_MEAN_ABS_DAILY_RETURN),
            "thresholds_exploratory_fixed_not_optimized": True,
            "regime_at_execution_time_has_no_current_execution_bar_leakage": True,
        },
        "portfolio": {
            "opening_cash": str(result.opening_cash),
            "ending_equity": str(result.ending_equity),
            "net_portfolio_pnl_usd_before_funding": str(result.total_pnl),
            "portfolio_return_before_funding": str(result.total_return),
            "closed_trades": result.closed_trade_count,
            "marked_days": len(rows),
            "marked_pnl_conserved": True,
            "closed_trades_conserved": True,
        },
        "by_trend": {
            key: _summarize(group, opening_cash=opening_cash)
            for key, group in sorted(btc_trend_rows.items())
        },
        "by_volatility": {
            key: _summarize(group, opening_cash=opening_cash)
            for key, group in sorted(btc_vol_rows.items())
        },
        "by_trend_volatility": {
            key: _summarize(group, opening_cash=opening_cash)
            for key, group in sorted(bucket_rows.items())
        },
        "limitations": [
            "Attribution of marked daily portfolio PnL, "
            "not an alternative regime-filtered strategy",
            "Prior BTC regime bins are descriptive fixed thresholds, not fitted market classifiers",
            "First execution days may be INSUFFICIENT_PRIOR_CONTEXT; preserved explicitly",
            "Original execution assumptions use daily OHLC and worst-case ambiguous exits",
            "Funding timing bounds are not allocated to daily PnL bins",
            "Regime values can be correlated with strategy PnL; association is not causality",
            "Same inspected history; no untouched OOS, live fills or strategy profitability proof",
            "Do not remove instruments or switch risk based on these historical regime bins",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bars-dir", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    payload = run_market_regime_attribution(
        bars_dir=args.bars_dir, symbols=tuple(sorted(args.symbols))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("PORTFOLIO", json.dumps(payload["portfolio"], sort_keys=True))
    for group, summary in sorted(payload["by_trend"].items()):
        print("TREND", group, json.dumps(summary, sort_keys=True))
    for group, summary in sorted(payload["by_volatility"].items()):
        print("VOLATILITY", group, json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
