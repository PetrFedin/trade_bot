"""Candidate B impulse-exhaustion *entry* hypotheses on inspected history.

Research only. Predeclared ATR-normalized prior one/two-day upside impulses
may veto NEW entries; prior selected positions and existing exit rules stay
unchanged. No signal ranking changes and no price from execution day is read.
Full portfolio counterfactual replays, not cherry-picked trade rewrites.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from app.marketdata.ohlcv import OhlcvBar
from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioEntryBlockReason,
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
CAPITAL = D("10000")
FEE_BPS = D("8")
SLIPPAGE_BPS = D("5")
ATR_LOOKBACK = 14
# Both rules were fixed before any historical outcome on this code branch.
HYPOTHESES = {
    "PRIOR_1D_ATR_150": (1, D("1.50")),
    "PRIOR_2D_ATR_225": (2, D("2.25")),
}


def completed_relative_atr(history: tuple[OhlcvBar, ...]) -> Decimal | None:
    """14 gap-adjusted true ranges / 14 / last completed close."""
    if len(history) < ATR_LOOKBACK + 1:
        return None
    tail = history[-ATR_LOOKBACK - 1:]
    if len({bar.symbol for bar in tail}) != 1:
        raise ValueError("mixed-symbol ATR history")
    for index, bar in enumerate(tail):
        bar.validate()
        if index and bar.timestamp <= tail[index - 1].timestamp:
            raise ValueError("ATR history must have increasing timestamps")
    true_ranges = (
        max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        for prev, curr in zip(tail, tail[1:], strict=False)
    )
    return (
        sum(true_ranges, D("0"))
        / D(ATR_LOOKBACK)
        / tail[-1].close
    )


def make_prior_impulse_gate(
    *,
    targeted_symbols: tuple[str, ...],
    hypothesis: str,
    audit: list[dict] | None = None,
):
    if not targeted_symbols or set(targeted_symbols) - set(SIX):
        raise ValueError("targeted symbols must be subset of fixed six-major universe")
    if len(set(targeted_symbols)) != len(targeted_symbols):
        raise ValueError("duplicate targeted symbol")
    if hypothesis not in HYPOTHESES:
        raise ValueError("unknown preregistered impulse profile")
    lookback, atr_multiplier = HYPOTHESES[hypothesis]
    target = frozenset(targeted_symbols)

    def allow_new_entry(symbol: str, completed: tuple[OhlcvBar, ...]) -> bool:
        if not completed or completed[-1].symbol != symbol:
            raise ValueError("entry gate requires prior completed symbol OHLC")
        if symbol not in SIX:
            raise ValueError("entry gate symbol not in study")
        if symbol not in target:
            return True
        atr = completed_relative_atr(completed)
        # No ATR => preserve baseline, never invent source values.
        if atr is None or atr <= 0:
            veto = False
            prior_impulse = None
        else:
            older = completed[-(lookback + 1)].close
            prior_impulse = completed[-1].close / older - D("1")
            veto = prior_impulse > atr * atr_multiplier
        if audit is not None:
            audit.append({
                "symbol": symbol,
                "hypothesis": hypothesis,
                "prior_complete_bar_utc": completed[-1].timestamp.isoformat(),
                "completed_history_bars": len(completed),
                "relative_atr14": None if atr is None else str(atr),
                "prior_upswing_fraction": (
                    None if prior_impulse is None else str(prior_impulse)
                ),
                "upside_threshold_atr": str(atr_multiplier),
                "vetoed_new_entry": veto,
                "old_position_exit_rules_unchanged": True,
            })
        return not veto

    return allow_new_entry


def _funding(result, schedules, synchronized, timeline):
    """Timing bounds, not exact venue settlements or mark-price funding."""
    slip = SLIPPAGE_BPS / D("10000")
    remaining = []
    for symbol, quantity in sorted(result.final_quantities.items()):
        if quantity <= 0:
            continue
        trace = next(
            (item for item in reversed(result.decision_trace)
             if symbol in item.entered_symbols),
            None,
        )
        if trace is None:
            raise ValueError("open holding missing its entry trace")
        entry_bar = next(
            (bar for bar in synchronized[symbol]
             if bar.timestamp == trace.execution_time),
            None,
        )
        if entry_bar is None:
            raise ValueError("open position entry has no sourced bar")
        remaining.append(FundingOpenPosition(
            symbol=symbol,
            entry_time=trace.execution_time,
            valuation_end=timeline[-1] + timedelta(days=1),
            quantity=quantity,
            entry_execution_price=entry_bar.open * (D("1") + slip),
        ))
    return funding_bounds_for_trades(
        result.closed_trades, schedules, open_positions=tuple(remaining),
    )


def _metrics(result, schedules, sync, timeline):
    funding = _funding(result, schedules, sync, timeline)
    if abs(
        result.ending_equity - result.opening_cash - result.total_pnl
    ) > D("0.000001"):
        raise ValueError("full portfolio cash/PnL identity invalid")
    if funding.lower_cost > funding.upper_cost:
        raise ValueError("funding timing interval inverted")
    for trade in result.closed_trades:
        expected = (
            trade.gross_pnl_before_costs - trade.slippage_cost
            - trade.entry_fee - trade.exit_fee
        )
        if abs(expected - trade.net_pnl) > D("0.00000001"):
            raise ValueError("trade fee/slippage accounting mismatch")
    return {
        "return_before_funding": str(result.total_return),
        "return_funding_lower": str(
            (result.total_pnl - funding.upper_cost) / CAPITAL
        ),
        "return_funding_upper": str(
            (result.total_pnl - funding.lower_cost) / CAPITAL
        ),
        "funding_cost_lower_usd": str(funding.lower_cost),
        "funding_cost_upper_usd": str(funding.upper_cost),
        "equity_before_funding_usd": str(result.ending_equity),
        "closed_trades": result.closed_trade_count,
        "entry_bar_exits": sum(
            t.holding_bars == 0 for t in result.closed_trades
        ),
        "ambiguous_intrabar_exits": sum(
            t.ambiguous_intrabar_exit for t in result.closed_trades
        ),
        "gap_through_stop_trades": sum(
            t.gap_through_stop for t in result.closed_trades
        ),
        "new_entry_veto_count": result.entry_block_counts.get(
            PortfolioEntryBlockReason.RESEARCH_PRIOR_ENTRY_VETO.value, 0
        ),
        "max_drawdown_fraction": str(result.max_drawdown_fraction),
        "turnover_fraction": str(result.turnover_fraction),
        "fees_paid_usd": str(result.fees_paid),
        "final_open_positions": sum(
            quantity > 0 for quantity in result.final_quantities.values()
        ),
    }


def run_impulse_lab(*, bars_dir: Path, funding_dir: Path) -> dict:
    original, sync, timeline, price_paths = run_shipped_portfolio(
        bars_dir=bars_dir, symbols=SIX, opening_cash=CAPITAL,
        fee_bps_per_fill=FEE_BPS, slippage_bps=SLIPPAGE_BPS,
    )
    if (
        original.closed_trade_count != 256
        or abs(original.total_return - D("0.007515525902184908204853186"))
        > D("0.00000001")
    ):
        raise ValueError("original frozen 6-major control mismatch")
    funding_paths = tuple(
        funding_dir / f"{symbol}_funding.csv" for symbol in SIX
    )
    if any(not path.is_file() for path in funding_paths):
        raise ValueError("original pinned funding source missing")
    funding_schedules = {
        symbol: load_funding_schedule(funding_dir / f"{symbol}_funding.csv")
        for symbol in SIX
    }
    capital_policy = CrossSectionalPortfolioPolicy(
        opening_cash=CAPITAL, fee_per_fill=D("0"),
        fee_bps_per_fill=FEE_BPS, slippage_bps=SLIPPAGE_BPS,
        maximum_gross_exposure_fraction=D("0.60"),
        new_position_target_equity_fraction=D("0.29"),
    )
    bars = [bar for symbol in SIX for bar in sync[symbol]]
    def replay(gate=None):
        return CrossSectionalPortfolioBacktester(
            selector=CrossSectionalSelector(top_k=2),
            portfolio_policy=capital_policy,
            position_policy=PositionManagementPolicy(),
            reentry_policy=ReentryConfirmationPolicy(
                minimum_consecutive_eligible_bars=2,
            ),
            research_prior_entry_veto=gate,
        ).run(bars)

    no_hook_control = replay()
    if no_hook_control != original:
        raise ValueError("entry hook changes shipped control with no gate")
    control = _metrics(original, funding_schedules, sync, timeline)
    experiments = []
    for universe in (*[(symbol,) for symbol in SIX], SIX):
        for hypothesis in HYPOTHESES:
            audit = []
            result = replay(make_prior_impulse_gate(
                targeted_symbols=universe,
                hypothesis=hypothesis,
                audit=audit,
            ))
            metrics = _metrics(result, funding_schedules, sync, timeline)
            veto_count = sum(x["vetoed_new_entry"] for x in audit)
            if veto_count != metrics["new_entry_veto_count"]:
                raise ValueError("veto audit does not reconcile to portfolio block count")
            if all(symbol in universe for symbol in SIX) and universe != SIX:
                raise ValueError("incorrect combined research universe")
            # Absence of veto MUST reproduce identical original decision trace and trades.
            if veto_count == 0 and result != original:
                raise ValueError("no-op gate unexpectedly changes original portfolio")
            experiments.append({
                "scope": "all_six_joint" if universe == SIX else "one_symbol_only",
                "targeted_symbols": list(universe),
                "hypothesis": hypothesis,
                "entry_impulse_veto_audit": audit,
                "vetoed_entries": veto_count,
                "all_rejected_entries_recorded": True,
                "unchanged_stop_target": True,
                "winner_selected": False,
                "delta_funding_lower_vs_control": str(
                    D(metrics["return_funding_lower"])
                    - D(control["return_funding_lower"])
                ),
                "delta_entry_bar_exits": (
                    metrics["entry_bar_exits"] - control["entry_bar_exits"]
                ),
                **metrics,
            })
    if len(experiments) != 14:
        raise ValueError("fixed 14-arm entry quality matrix incomplete")
    payload = {
        "schema_version": "astra-prior-impulse-entry-veto-research-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORICAL_ENTRY_RESEARCH_ONLY",
        "research_only": True,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "untouched_holdout": False,
        "parameter_winner_selected": False,
        "original_strategy_modified_in_production": False,
        "original_exit_rules_unchanged": True,
        "only_prior_completed_candles_for_entries": True,
        "full_portfolio_cash_replay_per_arm": True,
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((*price_paths, *funding_paths))
        },
        "parameters_predeclared": {
            hypothesis: {
                "prior_completed_return_bars": lookback,
                "veto_if_prior_positive_impulse_gt_atr_multiple": str(mult),
                "atr_true_range_bars": ATR_LOOKBACK,
            } for hypothesis, (lookback, mult) in HYPOTHESES.items()
        },
        "control": control,
        "experiments": experiments,
        "limitations": [
            "All historical dates and their failures were already inspected",
            "The veto only blocks entries; does not provide price improvement or later fill",
            "Model assumes deterministic top-two selection from completed previous candles",
            "Funding costs are uncertain bounds on constant entry notional",
            "5bps per side slippage/8bps fee are assumed, not verified venue fills",
            "Same daily candle stop+target ordering remains ambiguous",
            "An entry veto that loses fewer trades may still forgo upside, lower activity or capital",
            "Multiple comparison / hypothesis selection risk remains, no strategy is promoted",
            "A new untouched market period after frozen candidate and qualified feed is required",
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
    report = run_impulse_lab(
        bars_dir=args.bars_dir, funding_dir=args.funding_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print("ENTRY_IMPULSE_CONTROL", json.dumps(report["control"], sort_keys=True))
    for experiment in report["experiments"]:
        print("ENTRY_IMPULSE_ARM", json.dumps({
            key: experiment[key]
            for key in ("scope", "targeted_symbols", "hypothesis",
                        "return_funding_lower", "delta_funding_lower_vs_control",
                        "closed_trades", "entry_bar_exits", "vetoed_entries")
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
