"""Test whether a stop expressed in volatility behaves consistently across instruments.

The shipped policy stops at a fixed 2% regardless of what the instrument does. Against a
daily sigma of 3.4% that is inside one standard deviation, and 68% of episodes close in
their first bar: the position is resolved by one day of noise rather than by the thesis
that opened it. On a calmer instrument the same 2% is comparatively far away and rarely
touched. One number cannot mean the same thing on both.

Searching for a better fixed number has already been tested here and rejected: ranking
geometries on history correlated -0.13 with how they then performed. So this does not
search. It states a structural prediction that can fail, and checks it before looking at
any return:

    a stop placed at k times the instrument's own trailing volatility should produce a
    similar first-bar exit rate on every instrument, while a fixed fraction produces a
    rate that varies with that instrument's volatility.

The prediction concerns survival, not profit, so it cannot be satisfied by a lucky
sample of returns. If it fails, volatility scaling is not doing what the argument claims
and no P&L result from it should be trusted. Only if it holds is the return comparison
worth reading, and that comparison keeps the shipped reward-to-risk ratio so that the
stop placement is the only thing that changed.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.trading import Bar  # noqa: E402
from app.marketdata.ohlcv import OhlcvBar  # noqa: E402
from app.strategy.ohlcv_exit import (  # noqa: E402
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import PositionManagementPolicy  # noqa: E402
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy  # noqa: E402
from tools.replay_episodes import load_bars  # noqa: E402

SHIPPED = PositionManagementPolicy()
REWARD_TO_RISK = float(SHIPPED.take_profit_fraction / SHIPPED.stop_loss_fraction)
VOLATILITY_WINDOW = 20
DEFAULT_COST = 0.0016
# Floors and caps keep a scaled stop inside what the venue and the account can express.
MINIMUM_STOP = 0.005
MAXIMUM_STOP = 0.15


@dataclass(frozen=True)
class EpisodeResult:
    bars_held: int
    closed_first_bar: bool
    net_return: float
    stop_fraction: float


def trailing_sigma(bars: list[OhlcvBar], index: int, window: int = VOLATILITY_WINDOW) -> float:
    """Standard deviation of log returns over the bars strictly before index."""
    start = max(1, index - window)
    returns = [
        math.log(float(bars[i].close) / float(bars[i - 1].close))
        for i in range(start, index)
        if bars[i - 1].close > 0 and bars[i].close > 0
    ]
    return statistics.pstdev(returns) if len(returns) > 1 else 0.0


def scaled_policy(sigma: float, multiple: float) -> PositionManagementPolicy:
    """Build a policy whose stop is a multiple of sigma, keeping the shipped ratio."""
    stop = min(MAXIMUM_STOP, max(MINIMUM_STOP, sigma * multiple))
    target = stop * REWARD_TO_RISK
    activation = float(SHIPPED.trailing_activation_fraction / SHIPPED.stop_loss_fraction) * stop
    trailing = float(SHIPPED.trailing_stop_fraction / SHIPPED.stop_loss_fraction) * stop
    policy = PositionManagementPolicy(
        stop_loss_fraction=Decimal(str(round(stop, 8))),
        take_profit_fraction=Decimal(str(round(min(0.9, target), 8))),
        trailing_activation_fraction=Decimal(str(round(min(0.9, activation), 8))),
        trailing_stop_fraction=Decimal(str(round(trailing, 8))),
        maximum_holding_bars=SHIPPED.maximum_holding_bars,
    )
    policy.validate()
    return policy


def run(
    bars: list[OhlcvBar],
    *,
    multiple: float | None,
    cost: float = DEFAULT_COST,
) -> list[EpisodeResult]:
    """Replay one instrument; multiple None keeps the shipped fixed-fraction policy."""
    strategy = RegimeAwareMomentumStrategy()
    history = strategy.config.minimum_history_bars
    closes = [Bar(symbol=b.symbol, timestamp=b.timestamp, close=b.close) for b in bars]
    results: list[EpisodeResult] = []

    index = max(history, VOLATILITY_WINDOW)
    while index < len(bars) - 1:
        try:
            signal = strategy.signal(closes[max(0, index - history * 3) : index + 1])
        except ValueError:
            index += 1
            continue
        if not signal.eligible:
            index += 1
            continue

        if multiple is None:
            policy = SHIPPED
        else:
            sigma = trailing_sigma(bars, index)
            if sigma <= 0:
                index += 1
                continue
            policy = scaled_policy(sigma, multiple)

        entry_index = index + 1
        entry = bars[entry_index].open
        state = IntrabarPositionState(peak_completed_price=entry)
        held = 0
        realised = 0.0
        for offset in range(policy.maximum_holding_bars):
            position = entry_index + offset
            if position >= len(bars):
                break
            held = offset + 1
            decision = evaluate_long_intrabar_exit(
                average_cost=entry, bar=bars[position], state=state, policy=policy
            )
            if decision.exit_now:
                exit_price = decision.exit_price_before_costs or bars[position].close
                _ = IntrabarExitReason
                realised = float((exit_price - entry) / entry) - cost
                break
            state = decision.state
        else:
            closing = min(entry_index + policy.maximum_holding_bars - 1, len(bars) - 1)
            realised = float((bars[closing].close - entry) / entry) - cost

        results.append(
            EpisodeResult(
                bars_held=held,
                closed_first_bar=held <= 1,
                net_return=realised,
                stop_fraction=float(policy.stop_loss_fraction),
            )
        )
        index = entry_index + max(held, 1)
    return results


def _summary(per_symbol: dict[str, list[EpisodeResult]]) -> dict:
    first_bar_rates = []
    returns: list[float] = []
    stops: list[float] = []
    for results in per_symbol.values():
        if not results:
            continue
        first_bar_rates.append(sum(1 for r in results if r.closed_first_bar) / len(results))
        returns.extend(r.net_return for r in results)
        stops.extend(r.stop_fraction for r in results)
    if not returns:
        return {"symbols": 0}
    mean = statistics.fmean(returns)
    stdev = statistics.pstdev(returns)
    return {
        "symbols": len(first_bar_rates),
        "episodes": len(returns),
        "first_bar_exit_rate": {
            "mean": round(statistics.fmean(first_bar_rates), 6),
            "stdev_across_symbols": round(statistics.pstdev(first_bar_rates), 6),
            "min": round(min(first_bar_rates), 6),
            "max": round(max(first_bar_rates), 6),
        },
        "stop_fraction": {
            "mean": round(statistics.fmean(stops), 6),
            "min": round(min(stops), 6),
            "max": round(max(stops), 6),
        },
        "net_return": {
            "mean": round(mean, 8),
            "t": round(mean / (stdev / math.sqrt(len(returns))), 4) if stdev else None,
        },
    }


def study(datasets: list[Path], multiples: list[float], *, cost: float = DEFAULT_COST) -> dict:
    loaded = {path.name: load_bars(path) for path in datasets}
    arms: dict[str, dict] = {}
    arms["fixed (shipped)"] = _summary(
        {name: run(bars, multiple=None, cost=cost) for name, bars in loaded.items()}
    )
    for multiple in multiples:
        arms[f"sigma x{multiple:g}"] = _summary(
            {name: run(bars, multiple=multiple, cost=cost) for name, bars in loaded.items()}
        )

    fixed_spread = arms["fixed (shipped)"]["first_bar_exit_rate"]["stdev_across_symbols"]
    scaled = [
        value["first_bar_exit_rate"]["stdev_across_symbols"]
        for key, value in arms.items()
        if key != "fixed (shipped)" and value.get("symbols")
    ]
    return {
        "volatility_window_bars": VOLATILITY_WINDOW,
        "reward_to_risk_kept": REWARD_TO_RISK,
        "cost_fraction": cost,
        "arms": arms,
        "prediction": {
            "statement": (
                "A volatility-scaled stop should make the first-bar exit rate consistent "
                "across instruments; a fixed fraction should not."
            ),
            "fixed_stdev_across_symbols": fixed_spread,
            "scaled_stdev_across_symbols_min": round(min(scaled), 6) if scaled else None,
            "held": bool(scaled) and min(scaled) < fixed_spread,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--multiples", nargs="+", type=float, default=[0.5, 1.0, 1.5, 2.0])
    parser.add_argument("--cost-fraction", type=float, default=DEFAULT_COST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = study(args.datasets, args.multiples, cost=args.cost_fraction)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"volatility window {result['volatility_window_bars']} bars, "
        f"reward-to-risk held at {result['reward_to_risk_kept']:g}:1, "
        f"round trip {result['cost_fraction']:.2%}\n"
    )
    header = (
        f"{'arm':<20}{'episodes':>9}{'stop':>9}{'first bar':>11}"
        f"{'spread':>9}{'return':>10}{'t':>8}"
    )
    print(header)
    for name, arm in result["arms"].items():
        if not arm.get("symbols"):
            continue
        fb = arm["first_bar_exit_rate"]
        print(
            f"{name:<20}{arm['episodes']:>9}{arm['stop_fraction']['mean']:>9.2%}"
            f"{fb['mean']:>11.1%}{fb['stdev_across_symbols']:>9.3f}"
            f"{arm['net_return']['mean']:>+10.4%}{(arm['net_return']['t'] or 0):>+8.2f}"
        )
    prediction = result["prediction"]
    print(
        f"\n  prediction held: {'yes' if prediction['held'] else 'NO'}  "
        f"(fixed spread {prediction['fixed_stdev_across_symbols']:.3f}, "
        f"best scaled {prediction['scaled_stdev_across_symbols_min']:.3f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
