"""Test frozen strategy evidence against the baseline its own exit geometry implies.

A strategy evidence file records how often the take-profit was reached before the
protective stop. On its own that number says nothing: whether 22% is good or terrible
depends entirely on how far apart the two levels are. For a driftless random walk the
probability of touching the target first is

    p_baseline = stop_distance / (stop_distance + target_distance)

so a 2%/4% geometry makes 33.3% the coin-flip result, and anything at or below it is an
entry that adds nothing - or takes something away.

This turns that comparison into a gate. Promotion requires the observed rate to beat the
geometry's own baseline by a statistically significant margin AND to survive round-trip
costs. A strategy that is merely *positive* can still be worse than entering at random,
and a strategy that is significantly *below* baseline is not a tuning problem: its entry
carries information with the wrong sign, and stacking indicators on top of it compounds
the error rather than correcting it.

The gate is deliberately one-sided and deliberately dull. It cannot tell anyone which
strategy to build. It can only refuse to call a result an edge when the geometry already
explains it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "qualification" / "strategy"

# Round-trip taker cost as a fraction of notional. Bybit linear perpetual taker fees are
# 0.055% per side; the remainder is a deliberately unkind slippage allowance, because an
# edge that only survives optimistic fills is not an edge.
DEFAULT_COST_FRACTION = Decimal("0.0016")


class Verdict:
    BASELINE_UNDETERMINED = "BASELINE_UNDETERMINED"
    WORSE_THAN_RANDOM = "WORSE_THAN_RANDOM"
    INDISTINGUISHABLE = "INDISTINGUISHABLE_FROM_RANDOM"
    EDGE_PRESENT = "EDGE_PRESENT"


@dataclass(frozen=True)
class Geometry:
    stop_fraction: Decimal
    target_fraction: Decimal

    def validate(self) -> None:
        for name, value in (
            ("stop_fraction", self.stop_fraction),
            ("target_fraction", self.target_fraction),
        ):
            if not value.is_finite() or value <= 0 or value >= 1:
                raise ValueError(f"{name} must be finite and within (0, 1)")

    @property
    def reward_to_risk(self) -> float:
        return float(self.target_fraction / self.stop_fraction)

    @property
    def baseline_target_first(self) -> float:
        """Target-first probability for a driftless walk between the two levels."""
        return float(self.stop_fraction / (self.stop_fraction + self.target_fraction))


def policy_geometry() -> Geometry:
    """Read the geometry from the live position-management policy, not a copy of it."""
    # Importable both as `python -m tools.strategy_diagnostics` and as a plain script,
    # the way the other tools in this directory are invoked across the workflows.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.strategy.position_management import PositionManagementPolicy

    policy = PositionManagementPolicy()
    policy.validate()
    return Geometry(
        stop_fraction=policy.stop_loss_fraction,
        target_fraction=policy.take_profit_fraction,
    )


def policy_has_trailing_stop() -> bool:
    """Report whether the shipped policy moves the protective level while in profit."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.strategy.position_management import PositionManagementPolicy

    policy = PositionManagementPolicy()
    return policy.trailing_activation_fraction < policy.take_profit_fraction


def simulate_baseline(bar_sigma: float, *, episodes: int = 20000, seed: int = 20260917) -> float:
    """Measure the driftless target-first rate of the shipped exit policy.

    The closed form stop/(stop+target) describes two barriers that never move. The
    shipped policy moves one: once the position is ahead by the activation fraction the
    protective level rises to peak*(1 - trailing_fraction), so a position can be closed
    near breakeven without ever reaching the target. A bar where both levels are
    reachable is also resolved to the protective side. Both effects push the coin-flip
    rate well below the closed form, and neither is expressible in it.

    This runs driftless geometric Brownian paths through the production exit evaluator
    rather than a re-implementation, so the baseline reflects the policy that ships.
    """
    import random
    from datetime import UTC, datetime, timedelta

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.marketdata.ohlcv import OhlcvBar
    from app.strategy.ohlcv_exit import (
        IntrabarExitReason,
        IntrabarPositionState,
        evaluate_long_intrabar_exit,
    )
    from app.strategy.position_management import PositionManagementPolicy

    if not 0 < bar_sigma < 1:
        raise ValueError("bar_sigma must be within (0, 1)")

    policy = PositionManagementPolicy()
    policy.validate()
    rng = random.Random(seed)
    entry = Decimal("100")
    substeps = 24
    step_sigma = bar_sigma / (substeps**0.5)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    target_first = 0
    stop_first = 0
    for _ in range(episodes):
        state = IntrabarPositionState(peak_completed_price=entry)
        previous_close = 100.0
        for index in range(policy.maximum_holding_bars):
            price = previous_close
            high = low = price
            for _ in range(substeps):
                price *= 1.0 + rng.gauss(0.0, step_sigma)
                high = max(high, price)
                low = min(low, price)
            bar = OhlcvBar(
                symbol="SIM",
                timestamp=start + timedelta(days=index),
                open=Decimal(str(round(previous_close, 8))),
                high=Decimal(str(round(high, 8))),
                low=Decimal(str(round(low, 8))),
                close=Decimal(str(round(price, 8))),
                volume=1,
                trade_count=1,
            )
            decision = evaluate_long_intrabar_exit(
                average_cost=entry, bar=bar, state=state, policy=policy
            )
            if decision.exit_now:
                if decision.reason == IntrabarExitReason.TAKE_PROFIT:
                    target_first += 1
                else:
                    stop_first += 1
                break
            state = decision.state
            previous_close = price
    decided = target_first + stop_first
    return target_first / decided if decided else 0.0


def _display(path: Path) -> str:
    """Return a repository-relative path, or the full path when it lies outside."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _binomial_tail_at_most(successes: int, trials: int, probability: float) -> float:
    """P(X <= successes) for X ~ Binomial(trials, probability)."""
    return sum(
        math.comb(trials, index) * probability**index * (1 - probability) ** (trials - index)
        for index in range(successes + 1)
    )


def _binomial_tail_at_least(successes: int, trials: int, probability: float) -> float:
    """P(X >= successes) for X ~ Binomial(trials, probability)."""
    if successes == 0:
        return 1.0
    return 1.0 - _binomial_tail_at_most(successes - 1, trials, probability)


def expectancy_per_unit_risk(
    target_first_rate: float, geometry: Geometry, cost_fraction: Decimal
) -> float:
    """Expected result of one episode, measured in units of the stop distance.

    Costs are converted into the same unit: a round trip that costs 0.16% of notional
    consumes 0.08 of a 2% stop, and that is charged on every episode regardless of how
    it resolved.
    """
    cost_in_risk = float(cost_fraction / geometry.stop_fraction)
    win = target_first_rate * geometry.reward_to_risk
    loss = 1.0 - target_first_rate
    return win - loss - cost_in_risk


def diagnose(
    evidence: dict,
    geometry: Geometry,
    *,
    cost_fraction: Decimal = DEFAULT_COST_FRACTION,
    alpha: float = 0.01,
    bar_sigma: float | None = None,
    trailing_active: bool | None = None,
) -> dict:
    """Return a verdict on one frozen evidence record.

    When the shipped policy moves its protective level while in profit, the closed-form
    baseline does not describe it and a per-bar volatility is required to measure the
    baseline by simulation instead. Without one the verdict is BASELINE_UNDETERMINED:
    comparing against a baseline that does not match the policy produces a confident
    answer in whichever direction the mismatch happens to point.
    """
    geometry.validate()
    if trailing_active is None:
        trailing_active = policy_has_trailing_stop()

    target_first = int(evidence["target_first"])
    stop_first = int(evidence["stop_first"])
    decided = target_first + stop_first
    if decided <= 0:
        raise SystemExit("EVIDENCE_EMPTY: no resolved target/stop episodes")

    if trailing_active and bar_sigma is None:
        return {
            "verdict": Verdict.BASELINE_UNDETERMINED,
            "promotion_allowed": False,
            "reason": (
                "The shipped policy moves its protective level while in profit, so the "
                "closed-form baseline stop/(stop+target) does not describe it. Supply "
                "--bar-sigma to measure the baseline by simulating the real policy."
            ),
            "geometry": {
                "stop_fraction": str(geometry.stop_fraction),
                "target_fraction": str(geometry.target_fraction),
                "reward_to_risk": round(geometry.reward_to_risk, 4),
                "trailing_stop_active": True,
            },
            "episodes": {
                "target_first": target_first,
                "stop_first": stop_first,
                "resolved": decided,
            },
            "rates": {"observed_target_first": round(target_first / decided, 6)},
        }

    baseline = (
        simulate_baseline(bar_sigma) if bar_sigma is not None else geometry.baseline_target_first
    )
    observed = target_first / decided
    standard_error = math.sqrt(baseline * (1 - baseline) / decided)
    z_score = (observed - baseline) / standard_error if standard_error else 0.0

    p_below = _binomial_tail_at_most(target_first, decided, baseline)
    p_above = _binomial_tail_at_least(target_first, decided, baseline)

    observed_expectancy = expectancy_per_unit_risk(observed, geometry, cost_fraction)
    baseline_expectancy = expectancy_per_unit_risk(baseline, geometry, cost_fraction)

    if p_below <= alpha:
        verdict = Verdict.WORSE_THAN_RANDOM
    elif p_above <= alpha and observed_expectancy > 0:
        verdict = Verdict.EDGE_PRESENT
    else:
        verdict = Verdict.INDISTINGUISHABLE

    # The breakeven rate that the geometry and the cost model together demand.
    cost_in_risk = float(cost_fraction / geometry.stop_fraction)
    breakeven = (1.0 + cost_in_risk) / (1.0 + geometry.reward_to_risk)

    return {
        "verdict": verdict,
        "promotion_allowed": verdict == Verdict.EDGE_PRESENT,
        "geometry": {
            "stop_fraction": str(geometry.stop_fraction),
            "target_fraction": str(geometry.target_fraction),
            "reward_to_risk": round(geometry.reward_to_risk, 4),
            "trailing_stop_active": trailing_active,
        },
        "baseline_method": "simulated" if bar_sigma is not None else "closed-form",
        "bar_sigma": bar_sigma,
        "episodes": {
            "target_first": target_first,
            "stop_first": stop_first,
            "resolved": decided,
        },
        "rates": {
            "baseline_target_first": round(baseline, 6),
            "observed_target_first": round(observed, 6),
            "breakeven_target_first": round(breakeven, 6),
            "deviation_points": round((observed - baseline) * 100, 4),
        },
        "significance": {
            "z_score": round(z_score, 4),
            "p_worse_than_baseline": p_below,
            "p_better_than_baseline": p_above,
            "alpha": alpha,
        },
        "expectancy_per_unit_risk": {
            "observed": round(observed_expectancy, 6),
            "baseline": round(baseline_expectancy, 6),
            "cost_charged": round(cost_in_risk, 6),
        },
    }


def _format(report: dict, source: str) -> str:
    if report["verdict"] == Verdict.BASELINE_UNDETERMINED:
        episodes = report["episodes"]
        return "\n".join(
            [
                f"evidence      : {source}",
                f"episodes      : {episodes['resolved']} resolved "
                f"({episodes['target_first']} target-first, {episodes['stop_first']} stop-first)",
                f"observed rate : {report['rates']['observed_target_first']:.4f}",
                f"VERDICT       : {report['verdict']}",
                f"reason        : {report['reason']}",
            ]
        )
    rates = report["rates"]
    significance = report["significance"]
    expectancy = report["expectancy_per_unit_risk"]
    geometry = report["geometry"]
    episodes = report["episodes"]
    lines = [
        f"evidence      : {source}",
        f"geometry      : stop {geometry['stop_fraction']} / target "
        f"{geometry['target_fraction']}  (R:R {geometry['reward_to_risk']}:1)",
        f"episodes      : {episodes['resolved']} resolved "
        f"({episodes['target_first']} target-first, {episodes['stop_first']} stop-first)",
        f"baseline rate : {rates['baseline_target_first']:.4f}  "
        f"(what this geometry gives a driftless walk)",
        f"observed rate : {rates['observed_target_first']:.4f}  "
        f"({rates['deviation_points']:+.2f} points)",
        f"breakeven     : {rates['breakeven_target_first']:.4f}  (after costs)",
        f"significance  : z {significance['z_score']:+.2f}, "
        f"p(worse) {significance['p_worse_than_baseline']:.3e}, "
        f"p(better) {significance['p_better_than_baseline']:.3e}",
        f"expectancy    : {expectancy['observed']:+.4f} risk/episode "
        f"(a random entry here: {expectancy['baseline']:+.4f})",
        f"VERDICT       : {report['verdict']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "evidence",
        nargs="?",
        type=Path,
        help="strategy evidence JSON (default: every file in qualification/strategy)",
    )
    parser.add_argument("--stop-fraction", type=Decimal)
    parser.add_argument("--target-fraction", type=Decimal)
    parser.add_argument("--cost-fraction", type=Decimal, default=DEFAULT_COST_FRACTION)
    parser.add_argument("--alpha", type=float, default=0.01)
    parser.add_argument(
        "--bar-sigma",
        type=float,
        help=(
            "per-bar volatility used to simulate the baseline of the real exit policy; "
            "required whenever the policy carries a trailing stop"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--require-edge",
        action="store_true",
        help="exit non-zero unless every record shows a significant, cost-adjusted edge",
    )
    parser.add_argument(
        "--enforce-declared-promotion",
        action="store_true",
        help=(
            "exit non-zero when a record declares promotion_allowed while this gate finds "
            "no edge; the absence of an edge is not itself a failure"
        ),
    )
    args = parser.parse_args(argv)

    if (args.stop_fraction is None) != (args.target_fraction is None):
        raise SystemExit("GEOMETRY_INCOMPLETE: pass both --stop-fraction and --target-fraction")
    geometry = (
        Geometry(stop_fraction=args.stop_fraction, target_fraction=args.target_fraction)
        if args.stop_fraction is not None
        else policy_geometry()
    )

    if args.evidence is not None:
        paths = [args.evidence]
    else:
        paths = sorted(EVIDENCE_DIR.glob("*.json"))
    if not paths:
        raise SystemExit("NO_EVIDENCE_FOUND: qualification/strategy holds no records")

    reports = []
    for path in paths:
        evidence = json.loads(path.read_text())
        if "target_first" not in evidence or "stop_first" not in evidence:
            continue
        report = diagnose(
            evidence,
            geometry,
            cost_fraction=args.cost_fraction,
            alpha=args.alpha,
            bar_sigma=args.bar_sigma,
        )
        report["evidence_path"] = _display(path)
        report["declared_promotion_allowed"] = bool(evidence.get("promotion_allowed", False))
        reports.append(report)

    if not reports:
        raise SystemExit("NO_EPISODE_EVIDENCE: no record carries target_first/stop_first")

    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        print("\n\n".join(_format(report, report["evidence_path"]) for report in reports))

    if args.enforce_declared_promotion:
        # Today no record shows an edge, and that is a finding rather than a breakage.
        # What must never pass is a record claiming promotion the evidence cannot support.
        overclaimed = [
            r for r in reports if r["declared_promotion_allowed"] and not r["promotion_allowed"]
        ]
        if overclaimed:
            for report in overclaimed:
                print(
                    f"PROMOTION_OVERCLAIMED: {report['evidence_path']} declares "
                    f"promotion_allowed while the gate returns {report['verdict']}.",
                    file=sys.stderr,
                )
            return 1
        print(f"\nno record overclaims promotion ({len(reports)} checked)")

    if args.require_edge:
        failing = [r for r in reports if not r["promotion_allowed"]]
        if failing:
            print(
                f"\nSTRATEGY_EDGE_NOT_ESTABLISHED: {len(failing)} of {len(reports)} record(s) "
                f"do not beat the baseline their own exit geometry implies.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
