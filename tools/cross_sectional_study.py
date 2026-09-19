"""Test whether the entry signal pays as a cross-sectional spread rather than a direction.

The signal beats a random entry by a wide and robust margin, yet the strategy is flat,
because it is long-only in a universe whose median instrument fell over the window. That
combination has a specific reading: the signal may be ranking instruments well while the
direction it is forced to take gives the ranking back.

A spread separates the two. On each rebalance the instruments are ranked by the signal's
own momentum measure, the top fraction is held long and the bottom fraction short in
equal weight. Whatever moves the whole universe cancels between the legs, so what remains
is the ranking - and only the ranking.

Three controls run beside it. A long-only arm on the same top fraction shows what the
direction was costing. An equal-weight arm over the whole universe is the market the
spread is supposed to be neutral to. A shuffled-ranking arm, drawn many times, is what
the spread would earn if the signal ordered nothing.

Both legs pay the round trip, so a spread is charged twice what a single-sided position
is. That is a deliberately high bar and it is the honest one.

This reads data and computes returns. It touches no execution path, and a positive
result here would still be a research measurement rather than a promotion.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.trading import Bar  # noqa: E402
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy  # noqa: E402
from tools.replay_episodes import load_bars  # noqa: E402

DEFAULT_COST = 0.0016
DEFAULT_HOLD = 10
DEFAULT_FRACTION = 0.25
SHUFFLE_DRAWS = 200


@dataclass(frozen=True)
class Observation:
    date: str
    symbol: str
    score: float
    forward_return: float
    eligible: bool


def build_panel(
    datasets: list[Path], *, hold: int = DEFAULT_HOLD
) -> tuple[list[Observation], int]:
    """Score every symbol on every date and attach the return over the next hold bars.

    The score is the strategy's own momentum measure, read through its signal method, so
    the ranking is the shipped signal rather than a reconstruction of it. The forward
    return starts at the next bar's open: a ranking computed on a close cannot be traded
    at that close.
    """
    strategy = RegimeAwareMomentumStrategy()
    history = strategy.config.minimum_history_bars
    observations: list[Observation] = []
    skipped = 0

    for path in datasets:
        bars = load_bars(path)
        closes = [Bar(symbol=b.symbol, timestamp=b.timestamp, close=b.close) for b in bars]
        for index in range(history, len(bars) - hold - 1):
            try:
                signal = strategy.signal(closes[max(0, index - history * 3) : index + 1])
            except ValueError:
                skipped += 1
                continue
            entry = float(bars[index + 1].open)
            exit_price = float(bars[index + 1 + hold].open)
            if entry <= 0:
                skipped += 1
                continue
            observations.append(
                Observation(
                    date=bars[index].timestamp.date().isoformat(),
                    symbol=bars[index].symbol,
                    score=float(signal.momentum_return),
                    forward_return=(exit_price - entry) / entry,
                    eligible=bool(signal.eligible),
                )
            )
    return observations, skipped


def _by_date(observations: list[Observation]) -> dict[str, list[Observation]]:
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.date, []).append(observation)
    return grouped


def _rebalance_dates(dates: list[str], hold: int) -> list[str]:
    """Take every hold-th date so holding periods never overlap."""
    return dates[::hold]


def evaluate(
    observations: list[Observation],
    *,
    hold: int = DEFAULT_HOLD,
    fraction: float = DEFAULT_FRACTION,
    cost: float = DEFAULT_COST,
    minimum_breadth: int = 8,
    eligible_only: bool = False,
    shuffle_draws: int = SHUFFLE_DRAWS,
    seed: int = 20260917,
) -> dict:
    """Return spread, long-only, market and shuffled-ranking results per rebalance."""
    if not 0.1 <= fraction <= 0.5:
        raise ValueError("fraction must be within [0.1, 0.5]")
    grouped = _by_date(observations)
    dates = _rebalance_dates(sorted(grouped), hold)
    rng = random.Random(seed)

    spread: list[float] = []
    long_only: list[float] = []
    market: list[float] = []
    shuffled: list[float] = []

    for date in dates:
        rows = grouped[date]
        if len(rows) < minimum_breadth:
            continue
        size = max(1, int(len(rows) * fraction))
        ranked = sorted(rows, key=lambda row: row.score, reverse=True)
        if eligible_only:
            # The measured alpha came from the eligibility gate, not from the ordering,
            # so this restricts the long leg to instruments the shipped strategy would
            # actually have entered. The short leg keeps the full ranking: the gate says
            # nothing about what to sell.
            admitted = [row for row in ranked if row.eligible]
            if len(admitted) < 2:
                continue
            top = admitted[:size]
        else:
            top = ranked[:size]
        bottom = ranked[-size:]

        top_mean = statistics.fmean(row.forward_return for row in top)
        bottom_mean = statistics.fmean(row.forward_return for row in bottom)
        universe_mean = statistics.fmean(row.forward_return for row in rows)

        # Both legs pay the round trip; a single-sided position pays it once.
        spread.append(top_mean - bottom_mean - 2 * cost)
        long_only.append(top_mean - cost)
        market.append(universe_mean)

        for _ in range(max(1, shuffle_draws // max(1, len(dates)))):
            order = rows[:]
            rng.shuffle(order)
            fake_top = statistics.fmean(row.forward_return for row in order[:size])
            fake_bottom = statistics.fmean(row.forward_return for row in order[-size:])
            shuffled.append(fake_top - fake_bottom - 2 * cost)

    def summarise(values: list[float]) -> dict:
        if len(values) < 2:
            return {"periods": len(values), "mean": None, "t": None, "positive_share": None}
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values)
        return {
            "periods": len(values),
            "mean": round(mean, 8),
            "stdev": round(stdev, 8),
            "t": round(mean / (stdev / math.sqrt(len(values))), 4) if stdev else None,
            "positive_share": round(
                sum(1 for value in values if value > 0) / len(values), 4
            ),
        }

    correlation = None
    if len(spread) > 2 and len(spread) == len(market):
        mean_s, mean_m = statistics.fmean(spread), statistics.fmean(market)
        cov = statistics.fmean(
            (s - mean_s) * (m - mean_m) for s, m in zip(spread, market, strict=False)
        )
        sd_s, sd_m = statistics.pstdev(spread), statistics.pstdev(market)
        correlation = round(cov / (sd_s * sd_m), 4) if sd_s and sd_m else None

    return {
        "hold_bars": hold,
        "fraction_per_leg": fraction,
        "cost_fraction_per_leg_round_trip": cost,
        "spread_long_short": summarise(spread),
        "long_only_top": summarise(long_only),
        "market_equal_weight": summarise(market),
        "shuffled_ranking_control": summarise(shuffled),
        "spread_market_correlation": correlation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--hold", type=int, default=DEFAULT_HOLD)
    parser.add_argument("--fraction", type=float, default=DEFAULT_FRACTION)
    parser.add_argument("--cost-fraction", type=float, default=DEFAULT_COST)
    parser.add_argument("--holdout-fraction", type=float, default=0.0)
    parser.add_argument("--eligible-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    observations, skipped = build_panel(args.datasets, hold=args.hold)
    if not observations:
        print("no observations built", file=sys.stderr)
        return 1

    if args.holdout_fraction > 0:
        dates = sorted({item.date for item in observations})
        cut = dates[int(len(dates) * (1 - args.holdout_fraction))]
        observations = [item for item in observations if item.date >= cut]

    result = evaluate(
        observations,
        hold=args.hold,
        fraction=args.fraction,
        cost=args.cost_fraction,
        eligible_only=args.eligible_only,
    )
    result["observations"] = len(observations)
    result["skipped"] = skipped
    result["symbols"] = len({item.symbol for item in observations})

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"{result['symbols']} symbols, {result['observations']} observations, "
        f"hold {args.hold} bars, {args.fraction:.0%} per leg, "
        f"{args.cost_fraction:.2%} round trip per leg\n"
    )
    print(f"  {'arm':<30}{'periods':>9}{'mean':>11}{'t':>9}{'positive':>10}")
    for label, key in (
        ("spread, long minus short", "spread_long_short"),
        ("long only, top fraction", "long_only_top"),
        ("market, equal weight", "market_equal_weight"),
        ("shuffled ranking control", "shuffled_ranking_control"),
    ):
        block = result[key]
        if block["mean"] is None:
            print(f"  {label:<30}{block['periods']:>9}{'-':>11}{'-':>9}{'-':>10}")
            continue
        print(
            f"  {label:<30}{block['periods']:>9}{block['mean']:>+11.4%}"
            f"{(block['t'] or 0):>+9.2f}{(block['positive_share'] or 0):>10.1%}"
        )
    print(f"\n  spread to market correlation: {result['spread_market_correlation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
