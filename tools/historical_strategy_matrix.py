"""Run a deterministic cost-sensitivity matrix over local historical snapshots.

This tool performs no network calls. It reuses Historical Strategy Evidence v1 and
keeps every scenario research-only. It exists to make negative evidence visible:
absolute return is reported together with benchmark-relative gaps so a small positive
PnL cannot be mistaken for economic edge.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tools.historical_portfolio_evidence import build_evidence


@dataclass(frozen=True)
class CostScenario:
    scenario_id: str
    fee_per_fill: Decimal
    fee_bps_per_fill: Decimal
    slippage_bps: Decimal


SCENARIOS = (
    CostScenario("legacy_shadow", Decimal("0.50"), Decimal("0"), Decimal("5")),
    CostScenario("optimistic_taker", Decimal("0"), Decimal("6"), Decimal("3")),
    CostScenario("conservative_taker", Decimal("0"), Decimal("8"), Decimal("5")),
    CostScenario("stress_taker", Decimal("0"), Decimal("10"), Decimal("10")),
)


def _benchmark_return(payload: dict[str, object], benchmark_id: str) -> Decimal:
    for item in payload["benchmarks"]:
        if item["benchmark_id"] == benchmark_id:
            return Decimal(item["total_return"])
    raise ValueError(f"benchmark missing: {benchmark_id}")


def _summary(payload: dict[str, object]) -> dict[str, object]:
    strategy_return = Decimal(payload["total_return"])
    matched = _benchmark_return(payload, "equal_weight_capital_matched")
    btc = _benchmark_return(payload, "btc_buy_hold") if "BTCUSDT" in payload["symbols"] else None
    return {
        "ending_equity": payload["ending_equity"],
        "total_return": payload["total_return"],
        "max_drawdown_fraction": payload["max_drawdown_fraction"],
        "closed_trade_count": payload["closed_trade_count"],
        "win_rate": payload["win_rate"],
        "profit_factor": payload["profit_factor"],
        "turnover_fraction": payload["turnover_fraction"],
        "fees_paid": payload["fees_paid"],
        "maximum_gross_exposure_fraction": payload["maximum_gross_exposure_fraction"],
        "equal_weight_capital_matched_return": str(matched),
        "alpha_vs_capital_matched_equal_weight": str(strategy_return - matched),
        "btc_buy_hold_return": None if btc is None else str(btc),
        "alpha_vs_btc_buy_hold": None if btc is None else str(strategy_return - btc),
        "verdict": payload["verdict"],
        "evidence_sha256": payload["evidence_sha256"],
    }


def run_matrix(
    *,
    bars_dir: Path,
    symbols: tuple[str, ...],
    opening_cash: Decimal,
) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for scenario in SCENARIOS:
        payload = build_evidence(
            bars_dir=bars_dir,
            symbols=symbols,
            opening_cash=opening_cash,
            fee_per_fill=scenario.fee_per_fill,
            fee_bps_per_fill=scenario.fee_bps_per_fill,
            slippage_bps=scenario.slippage_bps,
        )
        scenarios[scenario.scenario_id] = {
            "costs": {
                "fee_per_fill": str(scenario.fee_per_fill),
                "fee_bps_per_fill": str(scenario.fee_bps_per_fill),
                "slippage_bps": str(scenario.slippage_bps),
            },
            "summary": _summary(payload),
            "evidence": payload,
        }

    return {
        "schema_version": "astra-historical-strategy-cost-matrix-v1",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "symbols": list(symbols),
        "opening_cash": str(opening_cash),
        "scenarios": scenarios,
        "interpretation_rule": (
            "Absolute profit is insufficient evidence. A scenario is economically weak "
            "when it materially underperforms the capital-matched passive benchmark, "
            "and all scenarios remain PROFITABILITY_NOT_PROVEN while funding coverage "
            "is incomplete."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--opening-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    symbols = tuple(sorted(args.symbols))
    payload = run_matrix(
        bars_dir=args.bars_dir,
        symbols=symbols,
        opening_cash=args.opening_cash,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
