"""Search exit geometries on real bars without letting the search fool itself.

Sweeping geometries and keeping the best is the exact failure this repository keeps
running into: a search over a fixed history always produces a winner, and the winner is
usually noise. Three defences are built in rather than recommended.

First, the window is split. Geometries are ranked on a development window and the
selection is scored on a holdout the search never touched. The gap between the two
numbers is what the search actually bought.

Second, every geometry is scored against a random-entry arm running the same geometry.
The difference isolates what the geometry does for *this signal* from what it does for
any entry at all - a geometry can look better simply by trading less.

Third, the holdout score of the selected geometry is compared against the holdout scores
of every geometry in the grid. Picking the maximum of N noisy estimates inflates it by
an amount that grows with N, so the reported significance is Holm-corrected across the
grid, and the spread of holdout results is printed so the reader can see whether the
winner stands apart or sits inside a cloud.

The shipped geometry is always in the grid and always reported. A search that cannot
beat the thing it is replacing has found nothing, however good its best row looks.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategy.position_management import PositionManagementPolicy  # noqa: E402
from tools.replay_episodes import load_bars, replay  # noqa: E402

DEFAULT_COST = Decimal("0.0016")
CONTROL_SEEDS = (101, 202)


@dataclass(frozen=True)
class Geometry:
    stop: str
    target: str
    activation: str
    trailing: str
    bars: int

    @property
    def name(self) -> str:
        return (
            f"{float(self.stop):.0%}/{float(self.target):.0%} "
            f"trail {float(self.activation):.1%}->{float(self.trailing):.1%} x{self.bars}"
        )

    def policy(self) -> PositionManagementPolicy:
        policy = PositionManagementPolicy(
            stop_loss_fraction=Decimal(self.stop),
            take_profit_fraction=Decimal(self.target),
            trailing_activation_fraction=Decimal(self.activation),
            trailing_stop_fraction=Decimal(self.trailing),
            maximum_holding_bars=self.bars,
        )
        policy.validate()
        return policy


SHIPPED = Geometry("0.02", "0.04", "0.020", "0.015", 10)


def grid() -> list[Geometry]:
    """The shipped geometry plus variations along one axis at a time, where possible.

    Kept deliberately modest. Every extra row raises the bar the winner must clear once
    the multiple-testing correction is applied, so an unfocused grid buys nothing.
    """
    items = [SHIPPED]
    for stop, target in (("0.02", "0.04"), ("0.03", "0.06"), ("0.04", "0.08"), ("0.03", "0.04")):
        for activation, trailing in (
            ("0.020", "0.015"),
            ("0.030", "0.025"),
            ("0.900", "0.015"),  # activation above target: trailing never engages
        ):
            for bars in (10, 20):
                candidate = Geometry(stop, target, activation, trailing, bars)
                if candidate != SHIPPED:
                    items.append(candidate)
    return items


def _score(bars, geometry: Geometry, cost: Decimal, *, control: bool) -> tuple[float, int]:
    """Mean net return per episode for one geometry on one slice, signal or control."""
    policy = geometry.policy()
    try:
        signal = replay(bars, policy=policy, cost_fraction=cost)
    except ValueError:
        return 0.0, 0
    if not control:
        mean = signal["realised"]["mean_return_per_episode"]
        return (mean or 0.0), signal["episodes"]
    if not signal["episodes"]:
        return 0.0, 0
    totals, counts = 0.0, 0
    for seed in CONTROL_SEEDS:
        run = replay(
            bars,
            policy=policy,
            cost_fraction=cost,
            random_entries=signal["episodes"],
            seed=seed,
        )
        mean = run["realised"]["mean_return_per_episode"]
        if run["episodes"]:
            totals += (mean or 0.0) * run["episodes"]
            counts += run["episodes"]
    return (totals / counts if counts else 0.0), counts


def _bootstrap(values: list[float], *, draws: int = 4000, seed: int = 20260917) -> tuple:
    """Percentile bootstrap interval for the mean of per-symbol results."""
    if len(values) < 3:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(statistics.fmean(sample))
    means.sort()
    return (round(means[int(0.025 * draws)], 8), round(means[int(0.975 * draws)], 8))


def _holm(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values, controlling the family-wise error rate."""
    ordered = sorted(pairs, key=lambda item: item[1])
    total = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (name, raw) in enumerate(ordered):
        value = min(1.0, (total - index) * raw)
        running = max(running, value)
        adjusted[name] = round(running, 6)
    return adjusted


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def search(
    datasets: list[Path],
    *,
    development_fraction: float = 0.7,
    cost: Decimal = DEFAULT_COST,
) -> dict:
    """Rank geometries on a development window and score the choice on a holdout."""
    if not 0.4 <= development_fraction <= 0.9:
        raise ValueError("development_fraction must be within [0.4, 0.9]")
    geometries = grid()
    loaded = [load_bars(path) for path in datasets]

    development: dict[str, list[float]] = {g.name: [] for g in geometries}
    holdout: dict[str, list[float]] = {g.name: [] for g in geometries}
    holdout_alpha: dict[str, list[float]] = {g.name: [] for g in geometries}

    for bars in loaded:
        split = int(len(bars) * development_fraction)
        dev_slice, hold_slice = bars[:split], bars[split:]
        for geometry in geometries:
            dev_mean, dev_episodes = _score(dev_slice, geometry, cost, control=False)
            if dev_episodes:
                development[geometry.name].append(dev_mean)
            hold_mean, hold_episodes = _score(hold_slice, geometry, cost, control=False)
            if not hold_episodes:
                continue
            holdout[geometry.name].append(hold_mean)
            control_mean, _ = _score(hold_slice, geometry, cost, control=True)
            holdout_alpha[geometry.name].append(hold_mean - control_mean)

    def summarise(values: list[float]) -> dict:
        if not values:
            return {"symbols": 0, "mean": None, "ci95": (None, None), "t": None}
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
        t = mean / (stdev / math.sqrt(len(values))) if stdev else 0.0
        return {
            "symbols": len(values),
            "mean": round(mean, 8),
            "ci95": _bootstrap(values),
            "t": round(t, 4),
        }

    rows = []
    for geometry in geometries:
        alpha = summarise(holdout_alpha[geometry.name])
        rows.append(
            {
                "geometry": geometry.name,
                "is_shipped": geometry == SHIPPED,
                "development": summarise(development[geometry.name]),
                "holdout": summarise(holdout[geometry.name]),
                "holdout_alpha_over_random": alpha,
            }
        )

    scored = [row for row in rows if row["development"]["mean"] is not None]
    selected = max(scored, key=lambda row: row["development"]["mean"]) if scored else None
    shipped_row = next(row for row in rows if row["is_shipped"])

    # One-sided p that each geometry's holdout alpha exceeds zero, Holm-corrected across
    # the grid, because the winner was chosen by looking at all of them.
    raw = [
        (row["geometry"], _normal_sf(row["holdout_alpha_over_random"]["t"] or 0.0))
        for row in rows
        if row["holdout_alpha_over_random"]["t"] is not None
    ]
    adjusted = _holm(raw)
    for row in rows:
        row["holdout_alpha_p_holm"] = adjusted.get(row["geometry"])

    holdout_means = [row["holdout"]["mean"] for row in rows if row["holdout"]["mean"] is not None]
    return {
        "grid_size": len(geometries),
        "development_fraction": development_fraction,
        "cost_fraction": str(cost),
        "selected_on_development": selected["geometry"] if selected else None,
        "selection_survived_holdout": (
            selected is not None
            and shipped_row["holdout"]["mean"] is not None
            and selected["holdout"]["mean"] is not None
            and selected["holdout"]["mean"] > shipped_row["holdout"]["mean"]
        ),
        "holdout_spread": {
            "min": round(min(holdout_means), 8) if holdout_means else None,
            "max": round(max(holdout_means), 8) if holdout_means else None,
            "stdev": round(statistics.pstdev(holdout_means), 8) if len(holdout_means) > 1 else None,
        },
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--development-fraction", type=float, default=0.7)
    parser.add_argument("--cost-fraction", type=Decimal, default=DEFAULT_COST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = search(
        args.datasets,
        development_fraction=args.development_fraction,
        cost=args.cost_fraction,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"{result['grid_size']} geometries, development {result['development_fraction']:.0%} "
        f"of each series, round trip {result['cost_fraction']}\n"
    )
    header = f"{'geometry':<38}{'dev':>10}{'holdout':>10}{'alpha':>10}{'p(Holm)':>10}"
    print(header)
    rows = sorted(
        result["rows"],
        key=lambda row: (row["development"]["mean"] is None, -(row["development"]["mean"] or 0)),
    )
    for row in rows[:14]:
        mark = " *" if row["is_shipped"] else ""
        dev = row["development"]["mean"]
        hold = row["holdout"]["mean"]
        alpha = row["holdout_alpha_over_random"]["mean"]
        p = row["holdout_alpha_p_holm"]
        print(
            f"{row['geometry'] + mark:<38}"
            f"{(dev if dev is not None else float('nan')):>10.4%}"
            f"{(hold if hold is not None else float('nan')):>10.4%}"
            f"{(alpha if alpha is not None else float('nan')):>10.4%}"
            f"{(p if p is not None else float('nan')):>10.4f}"
        )
    print("\n  * shipped geometry")
    print(f"  selected on development : {result['selected_on_development']}")
    print(
        f"  beat shipped on holdout : "
        f"{'yes' if result['selection_survived_holdout'] else 'NO'}"
    )
    spread = result["holdout_spread"]
    if spread["stdev"] is not None:
        print(
            f"  holdout spread          : {spread['min']:.4%} to {spread['max']:.4%}, "
            f"sd {spread['stdev']:.4%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
