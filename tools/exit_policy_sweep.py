"""Compare exit-policy geometries by how efficiently each converts drift into return.

A strategy has two separable parts: an entry that may or may not select moments with
favourable drift, and an exit policy that decides how much of that drift reaches the
account. They fail differently and must be measured separately. An entry with a real
edge can still lose money under a policy that closes winners early and losers late.

This measures the second part alone. Driftless paths and paths carrying a fixed drift
are run through the production exit evaluator, and each candidate geometry is scored by
the return it realises per episode. Under no drift a sound policy should give back
roughly the round-trip cost and no more - a policy that loses materially more than that
is taking something from every trade before the entry has said anything.

The shipped defaults are not changed here. Choosing a geometry is a decision for real
data: these numbers come from geometric Brownian paths, which carry none of the fat
tails, gaps or autocorrelation that decide whether a wider stop survives contact.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_COST_FRACTION = 0.0016
DEFAULT_BAR_SIGMA = 0.014
DEFAULT_DRIFT = 0.002
SUBSTEPS = 24


def _d(value: float) -> Decimal:
    return Decimal(str(round(value, 8)))


@dataclass(frozen=True)
class Candidate:
    name: str
    stop: float
    target: float
    trailing_activation: float
    trailing: float
    bars: int


# Geometries spanning the axes that matter: when the trailing stop engages, how far it
# trails, how wide the fixed levels sit, and how long a position is given to work.
CANDIDATES = (
    Candidate("shipped 2/4 trail 2.0/1.5 x10", 0.02, 0.04, 0.020, 0.015, 10),
    Candidate("trail later 2/4 trail 3.0/1.5", 0.02, 0.04, 0.030, 0.015, 10),
    Candidate("trail wider 2/4 trail 2.0/2.5", 0.02, 0.04, 0.020, 0.025, 10),
    Candidate("no trailing 2/4", 0.02, 0.04, 0.039, 0.015, 10),
    Candidate("closer target 2/3", 0.02, 0.03, 0.020, 0.015, 10),
    Candidate("wider stop 3/4 trail 2.5/2.0", 0.03, 0.04, 0.025, 0.020, 10),
    Candidate("symmetric 3/3 trail 2.0/2.0", 0.03, 0.03, 0.020, 0.020, 10),
    Candidate("shipped held x20", 0.02, 0.04, 0.020, 0.015, 20),
    Candidate("wider and longer 3/6 trail 3.0/2.5 x20", 0.03, 0.06, 0.030, 0.025, 20),
)


def _policy(candidate: Candidate):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.strategy.position_management import PositionManagementPolicy

    policy = PositionManagementPolicy(
        stop_loss_fraction=_d(candidate.stop),
        take_profit_fraction=_d(candidate.target),
        trailing_activation_fraction=_d(candidate.trailing_activation),
        trailing_stop_fraction=_d(candidate.trailing),
        maximum_holding_bars=candidate.bars,
    )
    policy.validate()
    return policy


def measure(
    candidate: Candidate,
    *,
    drift: float,
    bar_sigma: float = DEFAULT_BAR_SIGMA,
    cost_fraction: float = DEFAULT_COST_FRACTION,
    episodes: int = 12000,
    seed: int = 20260917,
) -> dict:
    """Return the realised per-episode result of one geometry under a given drift."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.marketdata.ohlcv import OhlcvBar
    from app.strategy.ohlcv_exit import IntrabarPositionState, evaluate_long_intrabar_exit

    if not 0 < bar_sigma < 1:
        raise ValueError("bar_sigma must be within (0, 1)")
    policy = _policy(candidate)
    rng = random.Random(seed)
    entry = 100.0
    step_sigma = bar_sigma / (SUBSTEPS**0.5)
    step_drift = drift / SUBSTEPS
    start = datetime(2026, 1, 1, tzinfo=UTC)

    total = 0.0
    squares = 0.0
    positive = 0
    for _ in range(episodes):
        state = IntrabarPositionState(peak_completed_price=_d(entry))
        previous_close = entry
        result = None
        for index in range(policy.maximum_holding_bars):
            price = previous_close
            high = low = price
            for _ in range(SUBSTEPS):
                price *= 1.0 + step_drift + rng.gauss(0.0, step_sigma)
                high = max(high, price)
                low = min(low, price)
            bar = OhlcvBar(
                symbol="SIM",
                timestamp=start + timedelta(days=index),
                open=_d(previous_close),
                high=_d(high),
                low=_d(low),
                close=_d(price),
                volume=1,
                trade_count=1,
            )
            decision = evaluate_long_intrabar_exit(
                average_cost=_d(entry), bar=bar, state=state, policy=policy
            )
            if decision.exit_now:
                exit_price = float(decision.exit_price_before_costs)
                result = (exit_price - entry) / entry - cost_fraction
                break
            state = decision.state
            previous_close = price
        if result is None:
            result = (previous_close - entry) / entry - cost_fraction
        total += result
        squares += result * result
        positive += int(result > 0)

    mean = total / episodes
    variance = squares / episodes - mean * mean
    return {
        "name": candidate.name,
        "mean_return": round(mean, 8),
        "positive_share": round(positive / episodes, 6),
        "return_over_dispersion": round(mean / variance**0.5, 6) if variance > 0 else 0.0,
    }


def sweep(
    *,
    drift: float = DEFAULT_DRIFT,
    bar_sigma: float = DEFAULT_BAR_SIGMA,
    cost_fraction: float = DEFAULT_COST_FRACTION,
    episodes: int = 12000,
) -> list[dict]:
    """Score every candidate under no drift and under the supplied drift."""
    rows = []
    for candidate in CANDIDATES:
        flat = measure(
            candidate,
            drift=0.0,
            bar_sigma=bar_sigma,
            cost_fraction=cost_fraction,
            episodes=episodes,
        )
        moving = measure(
            candidate,
            drift=drift,
            bar_sigma=bar_sigma,
            cost_fraction=cost_fraction,
            episodes=episodes,
        )
        rows.append(
            {
                "name": candidate.name,
                "driftless_return": flat["mean_return"],
                "drifted_return": moving["mean_return"],
                "positive_share": moving["positive_share"],
                "return_over_dispersion": moving["return_over_dispersion"],
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drift", type=float, default=DEFAULT_DRIFT, help="drift per bar")
    parser.add_argument("--bar-sigma", type=float, default=DEFAULT_BAR_SIGMA)
    parser.add_argument("--cost-fraction", type=float, default=DEFAULT_COST_FRACTION)
    parser.add_argument("--episodes", type=int, default=12000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = sweep(
        drift=args.drift,
        bar_sigma=args.bar_sigma,
        cost_fraction=args.cost_fraction,
        episodes=args.episodes,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(
        f"bar sigma {args.bar_sigma:.2%}, round trip {args.cost_fraction:.2%}, "
        f"drift {args.drift:+.2%} per bar, {args.episodes} episodes each\n"
    )
    header = f"{'geometry':<42}{'no drift':>10}{'with drift':>12}{'positive':>10}{'ret/disp':>10}"
    print(header)
    for row in rows:
        print(
            f"{row['name']:<42}{row['driftless_return']:>9.3%}"
            f"{row['drifted_return']:>11.3%}{row['positive_share']:>10.1%}"
            f"{row['return_over_dispersion']:>10.3f}"
        )
    best = max(rows, key=lambda row: row["drifted_return"])
    shipped = next(row for row in rows if row["name"].startswith("shipped 2/4"))
    ratio = best["drifted_return"] / shipped["drifted_return"] if shipped["drifted_return"] else 0
    print(
        f"\nbest under this drift: {best['name']} at {best['drifted_return']:.3%} per episode, "
        f"{ratio:.1f}x the shipped geometry."
    )
    print(
        "This says nothing about whether the entry carries drift at all. It says that if "
        "it does, the shipped geometry delivers a fraction of it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
