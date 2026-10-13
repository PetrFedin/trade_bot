"""Deterministic per-coin exit-policy experiments; NEVER execution authority.

Each experiment changes one coin's hard stop OR target, leaving five other
coins' rules, the selector, exposure, fees and chronological portfolio cash
unchanged. Original 6-major Strategy A is the independent control. Public
Bybit daily/funding archives are previously inspected; NO fresh OOS claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.historical_portfolio_funding import (
    FundingOpenPosition,
    funding_bounds_for_trades,
    load_funding_schedule,
)
from tools.historical_trade_attribution import run_shipped_portfolio

D = Decimal
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
CASH = D("10000")
FEE_BPS = D("8")
SLIP_BPS = D("5")

# Thresholds registered BEFORE evaluating any experiment's PnL.
# Vary ONE coordinate at a time: identify stop versus target effects.
EXIT_ARMS = {
    "STOP_1P": ("stop_loss_fraction", D("0.01")),
    "STOP_3P": ("stop_loss_fraction", D("0.03")),
    "TARGET_3P": ("take_profit_fraction", D("0.03")),
    "TARGET_6P": ("take_profit_fraction", D("0.06")),
}


def _trade_accounting_is_consistent(result) -> None:
    if abs(
        result.ending_equity - result.opening_cash - result.total_pnl
    ) > D("0.000001"):
        raise ValueError("portfolio equity cannot reconcile")
    for trade in result.closed_trades:
        expected = (
            trade.gross_pnl_before_costs - trade.slippage_cost
            - trade.entry_fee - trade.exit_fee
        )
        if abs(trade.net_pnl - expected) > D("0.00000001"):
            raise ValueError("trade costs do not reconcile")


def _funding_for_result(result, schedules, synchronized, timeline):
    """Bound funding including any final open positions at day-end.

    Uses existing timing-ambiguous, constant-entry-notional estimate,
    never reports account-exact funding or order-book executions.
    """
    remaining = []
    for symbol, quantity in sorted(result.final_quantities.items()):
        if quantity <= 0:
            continue
        trace = next(
            (day for day in reversed(result.decision_trace)
             if symbol in day.entered_symbols), None,
        )
        if trace is None:
            raise ValueError("open position missing entry trace")
        entry_bar = next(
            (bar for bar in synchronized[symbol]
             if bar.timestamp == trace.execution_time), None,
        )
        if entry_bar is None:
            raise ValueError("entry bar not in source")
        remaining.append(FundingOpenPosition(
            symbol=symbol,
            entry_time=trace.execution_time,
            valuation_end=timeline[-1] + timedelta(days=1),
            quantity=quantity,
            entry_execution_price=entry_bar.open * (D("1") + SLIP_BPS / D("10000")),
        ))
    return funding_bounds_for_trades(
        result.closed_trades, schedules,
        open_positions=tuple(remaining),
    )


def _row(result, *, funding, opening_cash):
    _trade_accounting_is_consistent(result)
    if funding.lower_cost > funding.upper_cost:
        raise ValueError("funding bounds inverted")
    return {
        "opening_cash_usd": str(result.opening_cash),
        "ending_equity_before_funding_usd": str(result.ending_equity),
        "net_portfolio_pnl_before_funding_usd": str(result.total_pnl),
        "return_before_funding": str(result.total_return),
        "funding_cost_lower_usd": str(funding.lower_cost),
        "funding_cost_upper_usd": str(funding.upper_cost),
        "return_with_funding_lower": str(
            (result.total_pnl - funding.upper_cost) / opening_cash
        ),
        "return_with_funding_upper": str(
            (result.total_pnl - funding.lower_cost) / opening_cash
        ),
        "closed_trades": result.closed_trade_count,
        "final_open_positions": sum(qty > 0 for qty in result.final_quantities.values()),
        "max_drawdown_fraction_before_funding": str(result.max_drawdown_fraction),
        "turnover_fraction": str(result.turnover_fraction),
        "fees_paid_usd": str(result.fees_paid),
        "ambiguous_daily_exit_count": sum(
            trade.ambiguous_intrabar_exit for trade in result.closed_trades
        ),
        "gap_through_stop_count": sum(
            trade.gap_through_stop for trade in result.closed_trades
        ),
        "same_entry_bar_exits": sum(
            trade.holding_bars == 0 for trade in result.closed_trades
        ),
    }


def run_per_asset_exit_matrix(
    *, bars_dir: Path, funding_dir: Path,
    symbols: tuple[str, ...] = SIX,
) -> dict:
    if symbols != SIX:
        raise ValueError("experimental universe must be exact six-major control")
    control, synchronized, timeline, price_paths = run_shipped_portfolio(
        bars_dir=bars_dir, symbols=symbols,
        opening_cash=CASH, fee_bps_per_fill=FEE_BPS,
        slippage_bps=SLIP_BPS,
    )
    if control.closed_trade_count != 256:
        raise ValueError("baseline unexpectedly diverged from frozen 256 trades")
    if abs(control.total_return - D(
        "0.007515525902184908204853186"
    )) > D("0.00000001"):
        raise ValueError("baseline equity differs from pinned original model")
    funding_paths = tuple(funding_dir / f"{s}_funding.csv" for s in symbols)
    if any(not p.is_file() for p in funding_paths):
        raise ValueError("missing genuine historical funding archive")
    schedules = {
        s: load_funding_schedule(funding_dir / f"{s}_funding.csv")
        for s in symbols
    }
    history = [
        bar for symbol in symbols for bar in synchronized[symbol]
    ]
    policy = CrossSectionalPortfolioPolicy(
        opening_cash=CASH,
        fee_per_fill=D("0"),
        fee_bps_per_fill=FEE_BPS,
        slippage_bps=SLIP_BPS,
        maximum_gross_exposure_fraction=D("0.60"),
        new_position_target_equity_fraction=D("0.29"),
    )
    baseline = _row(
        control, funding=_funding_for_result(
            control, schedules, synchronized, timeline,
        ), opening_cash=CASH,
    )
    experiments = []
    for symbol in symbols:
        for label, (field, value) in EXIT_ARMS.items():
            chosen = replace(PositionManagementPolicy(), **{field: value})
            chosen.validate()
            result = CrossSectionalPortfolioBacktester(
                selector=CrossSectionalSelector(top_k=2),
                portfolio_policy=policy,
                position_policy=PositionManagementPolicy(),
                position_policies_by_symbol={symbol: chosen},
                reentry_policy=ReentryConfirmationPolicy(
                    minimum_consecutive_eligible_bars=2,
                ),
            ).run(history)
            metrics = _row(result, funding=_funding_for_result(
                result, schedules, synchronized, timeline,
            ), opening_cash=CASH)
            experiments.append({
                "symbol": symbol,
                "arm": label,
                "changed_parameter": field,
                "changed_value": str(value),
                "all_other_symbols_unchanged": True,
                "position_policy_shipped_unchanged": True,
                "winner_selected": False,
                **metrics,
                "delta_vs_control_before_funding": str(
                    D(metrics["return_before_funding"])
                    - D(baseline["return_before_funding"])
                ),
                "delta_vs_control_funding_lower": str(
                    D(metrics["return_with_funding_lower"])
                    - D(baseline["return_with_funding_lower"])
                ),
            })
    if len(experiments) != 24 or any(
        arm["closed_trades"] < 0 for arm in experiments
    ):
        raise ValueError("24-arm preregistered experiment matrix incomplete")
    source_paths = price_paths + funding_paths
    payload = {
        "schema_version": "astra-per-asset-exit-policy-matrix-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_EXIT_POLICY_EXPLORATORY_ONLY",
        "research_only": True,
        "strategy_changed_in_production": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "independent_untouched_holdout": False,
        "parameter_winner_selected": False,
        "all_negatives_retained": True,
        "baseline_strategy_a_unmodified": True,
        "portfolio_counterfactuals_recomputed": True,
        "actual_order_fills_verified": False,
        "fee_bps_per_fill": str(FEE_BPS),
        "slippage_bps_per_side": str(SLIP_BPS),
        "funding_model": "TIMING_BOUNDS_CONSTANT_ENTRY_NOTIONAL_NOT_EXACT_FILL",
        "capital_usd": str(CASH),
        "symbols": list(symbols),
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_paths
        },
        "preregistered_arms": {
            name: {"parameter": field, "value": str(value)}
            for name, (field, value) in EXIT_ARMS.items()
        },
        "control": baseline,
        "experiments": experiments,
        "limitations": [
            "Full history was already inspected; all arms exploratory only",
            "Funding costs are timing bounds based on constant entry notional",
            "Intraday stop/target order is modelled conservatively at daily OHLC",
            "No orderbook, partial fills, spread time series or latency proof",
            "Capital reallocation after every changed exit is recomputed",
            "Observed historical winner is not an eligible production strategy",
            "A future untouched holdout after model freeze is indispensable",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--funding-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_per_asset_exit_matrix(
        bars_dir=args.bars_dir, funding_dir=args.funding_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    print("BASELINE", json.dumps(report["control"], sort_keys=True))
    for item in report["experiments"]:
        print("ARM", item["symbol"], item["arm"],
              item["return_with_funding_lower"],
              item["delta_vs_control_funding_lower"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
