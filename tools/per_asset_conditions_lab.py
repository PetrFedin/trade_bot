"""Research-only per-asset signal and exit-policy experiments.

Experiments are PREDECLARED for all symbols, not winners selected after viewing
historical PnL. Inputs must be already completed daily bars. Results are
in-sample diagnostics, never evidence of profitable deployable strategies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
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
from app.strategy.regime_momentum import RegimeAwareMomentumConfig
from tools.historical_portfolio_evidence import _common_timestamps

D = Decimal
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
# Every arm changes ONE symbol while all other symbols remain unchanged.
# These are illustrative, not parameters learned from outcomes.
ARMS = {
    "BASELINE": ("baseline", None),
    "STRICT_MOMENTUM": ("signal", "strict"),
    "RELAXED_MOMENTUM": ("signal", "relaxed"),
}
FEE_BPS = D("8")
SLIP_BPS = D("5")


def _signal_variant(kind: str) -> RegimeAwareMomentumConfig:
    base = RegimeAwareMomentumConfig()
    if kind == "strict":
        return replace(base, minimum_momentum_return=D("0.008"),
                       minimum_trend_strength=D("0.004"),
                       maximum_realized_volatility=D("0.02"))
    if kind == "relaxed":
        return replace(base, minimum_momentum_return=D("0"),
                       minimum_trend_strength=D("0"),
                       maximum_realized_volatility=D("0.05"))
    raise ValueError("unknown preregistered signal variant")


class SingleAssetResearchSelector(CrossSectionalSelector):
    """Use per-asset completed-bar signal, preserve common selection ranking."""

    def __init__(self, *, experiment_symbol: str, variant: str):
        super().__init__(top_k=2)
        self.experiment_symbol = experiment_symbol
        self.variant = variant

    def select(self, bars):
        # Feed the ordinary selector unchanged for the baseline control.
        # The alternate signal is injected through a separate per-symbol
        # evaluation while keeping the original ranking and history clock.
        from collections import defaultdict

        from app.domain.trading import Bar
        from app.strategy.cross_sectional_selection import (
            CrossSectionalSelection,
            SelectionCandidate,
        )
        from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

        grouped = defaultdict(list)
        for bar in bars:
            bar.validate()
            grouped[bar.symbol].append(bar)
        if self.experiment_symbol not in grouped:
            raise ValueError("experimental symbol missing")
        decision_times = {max(b.timestamp for b in x) for x in grouped.values()}
        if len(decision_times) != 1:
            raise ValueError("unsynchronized decision clock")
        scored = []
        for symbol, history in sorted(grouped.items()):
            ordered = sorted(history, key=lambda x: x.timestamp)
            config = (_signal_variant(self.variant) if symbol == self.experiment_symbol
                      else self.signal_config)
            if len(ordered) < config.minimum_history_bars:
                raise ValueError("insufficient prior history")
            signal = RegimeAwareMomentumStrategy(
                config=config, target_quantity=D("1"),
            ).signal([Bar(symbol=symbol, timestamp=b.timestamp, close=b.close)
                      for b in ordered])
            scored.append((symbol, signal, ordered[-1].close))
        eligible = [row for row in scored if row[1].eligible]
        eligible.sort(key=lambda r: (-r[1].momentum_return, -r[1].trend_strength,
                                     r[1].realized_volatility, r[0]))
        ranks = {s: i + 1 for i, (s, _, _) in enumerate(eligible)}
        return CrossSectionalSelection(
            decision_time=next(iter(decision_times)),
            selected_symbols=tuple(s for s, _, _ in eligible[:self.top_k]),
            candidates=tuple(SelectionCandidate(
                rank=ranks.get(s), symbol=s, eligible=sig.eligible,
                rejection_reasons=sig.reasons,
                momentum_return=sig.momentum_return,
                trend_strength=sig.trend_strength,
                realized_volatility=sig.realized_volatility,
                reference_price=price,
            ) for s, sig, price in scored),
        )


def run_experiments(*, bars_dir: Path, symbols: tuple[str, ...] = SIX) -> dict:
    if symbols != SIX:
        raise ValueError("first experiment fixed to six majors")
    paths = [bars_dir / f"{s}_D.csv" for s in symbols]
    loaded, timeline = _common_timestamps(paths)
    common = set(timeline)
    bars = [bar for s in symbols for bar in loaded[s] if bar.timestamp in common]
    policy = CrossSectionalPortfolioPolicy(
        opening_cash=D("10000"), fee_per_fill=D("0"),
        fee_bps_per_fill=FEE_BPS, slippage_bps=SLIP_BPS,
        maximum_gross_exposure_fraction=D("0.60"),
        new_position_target_equity_fraction=D("0.29"),
    )

    def replay(selector):
        r = CrossSectionalPortfolioBacktester(
            selector=selector, portfolio_policy=policy,
            position_policy=PositionManagementPolicy(),
            reentry_policy=ReentryConfirmationPolicy(
                minimum_consecutive_eligible_bars=2),
        ).run(bars)
        return {
            "return_before_funding": str(r.total_return),
            "ending_equity_usd": str(r.ending_equity),
            "closed_trades": r.closed_trade_count,
            "max_drawdown_fraction": str(r.max_drawdown_fraction),
            "turnover_fraction": str(r.turnover_fraction),
            "fees_paid_usd": str(r.fees_paid),
            "original_strategy_funding_not_included": True,
        }
    control = replay(CrossSectionalSelector(top_k=2))
    experiments = []
    for symbol in symbols:
        for variant in ("strict", "relaxed"):
            value = replay(SingleAssetResearchSelector(
                experiment_symbol=symbol, variant=variant))
            experiments.append({
                "symbol": symbol, "variant": variant, **value,
                "delta_vs_control_before_funding": str(
                    D(value["return_before_funding"])
                    - D(control["return_before_funding"])),
                "winner_selected": False,
            })
    payload = {
        "schema_version": "astra-per-asset-conditions-research-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_EXPLORATORY_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "untouched_holdout": False,
        "funding_modelled": False,
        "lookahead_current_execution_bar_used": False,
        "experiment_grid": "six symbols x two predeclared signal profiles",
        "parameters_are_not_fitted_to_history": True,
        "winner_selected": False,
        "capital_usd": "10000",
        "fee_bps_per_fill": str(FEE_BPS),
        "slippage_bps_per_side": str(SLIP_BPS),
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
        "control": control,
        "experiments": experiments,
        "limitations": [
            "Fixed full-sample exploratory replay is not nested walk-forward optimization",
            "No funding bounds in this early signal experiment; do not claim net edge",
            "Different trades change selection, cash and future opportunities",
            "All historical daily candles already inspected; future holdout is reserved",
            "Intrabar OHLC collision remains conservatively stop-first",
            "Optimization or deployment from ranked historical winners is prohibited",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_experiments(bars_dir=args.bars_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("CONTROL", report["control"])
    for arm in report["experiments"]:
        print("ARM", arm["symbol"], arm["variant"],
              arm["return_before_funding"], arm["closed_trades"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
