"""Entry-frozen, prior-bar ATR exit-policy diagnostics for six crypto assets.

Every historical arm runs a complete synchronous portfolio. The ATR input
contains ONLY the symbol's completed bars before the execution day's open.
The derived immutable PositionManagementPolicy is frozen for that holding.
Data was previously inspected: NO untouched OOS or live-trading evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioDecisionTrace,
)
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    CrossSectionalSelector,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.historical_portfolio_funding import load_funding_schedule
from tools.historical_trade_attribution import run_shipped_portfolio
from tools.per_asset_exit_policy_matrix import (
    CASH,
    FEE_BPS,
    SIX,
    SLIP_BPS,
    _funding_for_result,
    _row,
)

D = Decimal
ATR_LOOKBACK = 14
# Fixed before historical arm evaluation; ranges are policy limits, not fitted optima.
ATR_ARMS = {
    "ATR_COMPACT": (D("0.50"), D("1.00")),
    "ATR_EXTENDED": (D("0.75"), D("1.50")),
}
STOP_FLOOR = D("0.01")
STOP_CEILING = D("0.04")
TARGET_FLOOR = D("0.02")
TARGET_CEILING = D("0.08")


@dataclass(frozen=True)
class AtrEntryProfile:
    stop_multiplier: Decimal
    target_multiplier: Decimal

    def validate(self) -> None:
        for name, value in (
            ("stop_multiplier", self.stop_multiplier),
            ("target_multiplier", self.target_multiplier),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


def _bounded(value: Decimal, floor: Decimal, ceiling: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("ATR-derived fraction must be finite")
    return min(ceiling, max(floor, value))


def atr_fraction_from_completed(
    completed_bars: tuple[OhlcvBar, ...], *, periods: int = ATR_LOOKBACK
) -> Decimal | None:
    """ATR / previous completed close; excludes execution bar by contract.

    Need N true ranges and N+1 completed OHLC bars (previous-close gaps).
    For insufficient history, report None rather than silently using fewer
    periods or current-day information.
    """
    if periods < 2:
        raise ValueError("ATR periods must be at least two")
    if len(completed_bars) < periods + 1:
        return None
    tail = completed_bars[-periods - 1:]
    if len({bar.symbol for bar in tail}) != 1:
        raise ValueError("ATR history must contain exactly one symbol")
    if any(
        tail[i - 1].timestamp >= tail[i].timestamp
        for i in range(1, len(tail))
    ):
        raise ValueError("ATR completed input must be strictly chronological")
    for bar in tail:
        bar.validate()
    total = D("0")
    for previous, current in zip(tail, tail[1:], strict=False):
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        total += true_range
    reference = tail[-1].close
    if reference <= 0:
        raise ValueError("ATR reference close must be positive")
    return total / D(periods) / reference


def fixed_entry_policy(
    *, completed_bars: tuple[OhlcvBar, ...], profile: AtrEntryProfile
) -> tuple[PositionManagementPolicy, Decimal | None]:
    profile.validate()
    atr_fraction = atr_fraction_from_completed(completed_bars)
    original = PositionManagementPolicy()
    if atr_fraction is None:
        return original, None
    new_policy = replace(
        original,
        stop_loss_fraction=_bounded(
            atr_fraction * profile.stop_multiplier, STOP_FLOOR, STOP_CEILING
        ),
        take_profit_fraction=_bounded(
            atr_fraction * profile.target_multiplier, TARGET_FLOOR, TARGET_CEILING
        ),
    )
    new_policy.validate()
    return new_policy, atr_fraction


class FrozenHistoricalSelections(CrossSectionalSelector):
    """Replay exactly the selection already computed by original Strategy A.

    This caches only the strategy's time-indexed prior-bar selections,
    never trade outcomes or lookahead information. Separate parity check
    proves the baseline cash ledger is unchanged.
    """

    def __init__(
        self, trace: tuple[PortfolioDecisionTrace, ...], *, symbols: tuple[str, ...]
    ) -> None:
        super().__init__(top_k=2)
        if len(trace) < 1 or len(set(symbols)) != len(symbols):
            raise ValueError("frozen selection needs complete symbol timeline")
        self.symbols = tuple(sorted(symbols))
        self.by_time = {
            item.decision_time: (item.execution_index, item.selected_symbols)
            for item in trace
        }
        if len(self.by_time) != len(trace):
            raise ValueError("duplicate frozen decision time")
        if any(
            len(selected) > self.top_k or len(set(selected)) != len(selected)
            or set(selected) - set(self.symbols)
            for _, selected in self.by_time.values()
        ):
            raise ValueError("invalid frozen selected symbols")

    def select(self, bars) -> CrossSectionalSelection:
        if not bars or bars[-1].symbol != self.symbols[-1]:
            raise ValueError("frozen research history symbol order changed")
        completed_time = bars[-1].timestamp
        if completed_time not in self.by_time:
            raise ValueError("unregistered decision time for frozen selection")
        execution_index, selected = self.by_time[completed_time]
        if len(bars) != execution_index * len(self.symbols):
            raise ValueError("incomplete or altered selection history")
        return CrossSectionalSelection(
            decision_time=completed_time,
            selected_symbols=selected,
            candidates=(),
        )


def run_atr_matrix(*, bars_dir: Path, funding_dir: Path) -> dict:
    original, synchronized, timeline, bar_paths = run_shipped_portfolio(
        bars_dir=bars_dir,
        symbols=SIX,
        opening_cash=CASH,
        fee_bps_per_fill=FEE_BPS,
        slippage_bps=SLIP_BPS,
    )
    if original.closed_trade_count != 256 or abs(
        original.total_return - D("0.007515525902184908204853186")
    ) > D("0.00000001"):
        raise ValueError("frozen Strategy A control diverged")
    funding_paths = tuple(funding_dir / f"{symbol}_funding.csv" for symbol in SIX)
    if any(not path.is_file() for path in funding_paths):
        raise ValueError("funding source missing")
    schedules = {
        symbol: load_funding_schedule(funding_dir / f"{symbol}_funding.csv")
        for symbol in SIX
    }
    history = [bar for symbol in SIX for bar in synchronized[symbol]]
    common_policy = CrossSectionalPortfolioPolicy(
        opening_cash=CASH,
        fee_per_fill=D("0"),
        fee_bps_per_fill=FEE_BPS,
        slippage_bps=SLIP_BPS,
        maximum_gross_exposure_fraction=D("0.60"),
        new_position_target_equity_fraction=D("0.29"),
    )
    frozen_selector = FrozenHistoricalSelections(
        original.decision_trace, symbols=SIX,
    )

    def replay(resolver=None):
        return CrossSectionalPortfolioBacktester(
            selector=frozen_selector,
            portfolio_policy=common_policy,
            position_policy=PositionManagementPolicy(),
            reentry_policy=ReentryConfirmationPolicy(
                minimum_consecutive_eligible_bars=2,
            ),
            research_entry_policy_resolver=resolver,
        ).run(history)

    cached_control = replay()
    if (
        cached_control.ending_equity != original.ending_equity
        or cached_control.closed_trades != original.closed_trades
        or cached_control.decision_trace != original.decision_trace
    ):
        raise ValueError("frozen selection cache not exactly identical to shipped A")
    baseline = _row(
        original,
        funding=_funding_for_result(original, schedules, synchronized, timeline),
        opening_cash=CASH,
    )
    experiments = []
    for symbol in SIX:
        for arm_name, (stop_mult, target_mult) in ATR_ARMS.items():
            audit = []
            profile = AtrEntryProfile(stop_mult, target_mult)

            def entry_policy(
                selected_symbol: str, completed: tuple[OhlcvBar, ...]
            ) -> PositionManagementPolicy:
                if selected_symbol != symbol:
                    return PositionManagementPolicy()
                derived, atr_fraction = fixed_entry_policy(
                    completed_bars=completed, profile=profile,
                )
                audit.append({
                    "symbol": selected_symbol,
                    "decision_bar_timestamp": completed[-1].timestamp.isoformat(),
                    "prior_completed_bars": len(completed),
                    "atr_fraction": None if atr_fraction is None else str(atr_fraction),
                    "stop_fraction": str(derived.stop_loss_fraction),
                    "target_fraction": str(derived.take_profit_fraction),
                    "insufficient_atr_history_fallback": atr_fraction is None,
                    "policy_frozen_at_entry": True,
                })
                return derived

            result = replay(entry_policy)
            metrics = _row(
                result,
                funding=_funding_for_result(result, schedules, synchronized, timeline),
                opening_cash=CASH,
            )
            experiments.append({
                "symbol": symbol,
                "arm": arm_name,
                "atr_lookback_completed_bars": ATR_LOOKBACK,
                "stop_atr_multiplier": str(stop_mult),
                "target_atr_multiplier": str(target_mult),
                "policy_floor_stop": str(STOP_FLOOR),
                "policy_ceiling_stop": str(STOP_CEILING),
                "policy_floor_target": str(TARGET_FLOOR),
                "policy_ceiling_target": str(TARGET_CEILING),
                "winner_selected": False,
                "atr_entry_policy_audit": audit,
                "atr_fallback_entries": sum(
                    bool(x["insufficient_atr_history_fallback"]) for x in audit
                ),
                "delta_vs_control_before_funding": str(
                    D(metrics["return_before_funding"])
                    - D(baseline["return_before_funding"])
                ),
                "delta_vs_control_funding_lower": str(
                    D(metrics["return_with_funding_lower"])
                    - D(baseline["return_with_funding_lower"])
                ),
                **metrics,
            })
    if len(experiments) != 12:
        raise ValueError("12 preregistered ATR arms not completed")
    source_paths = bar_paths + funding_paths
    payload = {
        "schema_version": "astra-entry-frozen-atr-exits-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_ATR_HYPOTHESIS_EXPLORATORY",
        "research_only": True,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "out_of_sample": False,
        "new_holdout_performed": False,
        "actual_exchange_fills_observed": False,
        "winner_selected": False,
        "strategy_a_control_exact_parity": True,
        "cash_path_replayed_for_every_arm": True,
        "per_entry_policy_frozen": True,
        "current_execution_bar_excluded_from_atr": True,
        "funding_bounds_are_modelled_not_exchange_settlements": True,
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_paths
        },
        "control": baseline,
        "experiments": experiments,
        "limitations": [
            "Already inspected history; all PnL comparisons hypothesis-generating",
            "Daily OHLC stop/take ordering remains conservative but unverified",
            "Funding is bounded on constant entry notional, not true exchange fills",
            "Orderbook depth/latency/partial fills not available",
            "ATR caps and multipliers are predeclared exploratory choices",
            "A nested chronological walk-forward and genuinely new holdout remain",
            "Research result may never enable broker/OMS trading",
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
    report = run_atr_matrix(
        bars_dir=args.bars_dir, funding_dir=args.funding_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("FROZEN_ATR", json.dumps({
        "control_return": report["control"]["return_with_funding_lower"],
        "arms": [
            {"symbol": x["symbol"], "arm": x["arm"],
             "return_with_funding_lower": x["return_with_funding_lower"],
             "closed_trades": x["closed_trades"]}
            for x in report["experiments"]
        ],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
