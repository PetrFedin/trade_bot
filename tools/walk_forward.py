"""Measure how much of a tuned result survives on data it was not tuned on.

Any parameter search over a fixed history produces a positive number. The question is
never whether tuning finds something - it always does - but how much of it is left when
the same parameters meet bars the search never saw. That difference is the only honest
estimate of what tuning bought, and it is what this reports.

Each fold selects parameters on a training window by mean realised return, then applies
them unchanged to the test window that immediately follows and never overlaps it. The
in-sample figure is what the search believed; the out-of-sample figure is what survived.
A large gap between them is overfitting measured rather than argued.

A fixed-parameter arm runs the shipped configuration through the identical fold
structure. When tuning cannot beat it out of sample, the search is finding noise: the
comparison is what stops a tuned number from being read as an improvement on its own.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategy.regime_momentum import (  # noqa: E402
    RegimeAwareMomentumConfig,
    RegimeAwareMomentumStrategy,
)
from tools.replay_episodes import load_bars, replay  # noqa: E402

# A deliberately small grid. Every additional axis multiplies the number of ways the
# search can fit noise, and the point here is to measure that cost, not to maximise it.
MOMENTUM_THRESHOLDS = (Decimal("0.001"), Decimal("0.002"), Decimal("0.005"))
VOLATILITY_CAPS = (Decimal("0.03"), Decimal("0.05"), Decimal("0.08"))


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: int
    train_end: int
    test_end: int


def folds(total_bars: int, *, train: int, test: int) -> list[Fold]:
    """Return non-overlapping test windows, each preceded by its own training window."""
    if train < 60 or test < 20:
        raise ValueError("train must be at least 60 bars and test at least 20")
    result = []
    index = 0
    start = 0
    while start + train + test <= total_bars:
        result.append(
            Fold(
                index=index,
                train_start=start,
                train_end=start + train,
                test_end=start + train + test,
            )
        )
        start += test
        index += 1
    return result


def grid() -> list[RegimeAwareMomentumConfig]:
    base = RegimeAwareMomentumConfig()
    return [
        replace(base, minimum_momentum_return=momentum, maximum_realized_volatility=cap)
        for momentum in MOMENTUM_THRESHOLDS
        for cap in VOLATILITY_CAPS
    ]


def _score(bars, config: RegimeAwareMomentumConfig, cost: Decimal) -> tuple[float, int]:
    """Return mean realised return per episode and the episode count for one slice."""
    try:
        report = replay(
            bars, strategy=RegimeAwareMomentumStrategy(config=config), cost_fraction=cost
        )
    except ValueError:
        return 0.0, 0
    mean = report["realised"]["mean_return_per_episode"]
    return (mean if mean is not None else 0.0), report["episodes"]


def run(
    datasets: list[Path],
    *,
    train: int = 365,
    test: int = 120,
    cost_fraction: Decimal = Decimal("0.0016"),
    minimum_train_episodes: int = 12,
) -> dict:
    """Walk every dataset fold by fold, tuning on train and measuring on test."""
    candidates = grid()
    shipped = RegimeAwareMomentumConfig()

    tuned_is: list[float] = []
    tuned_oos: list[float] = []
    fixed_oos: list[float] = []
    rows = []

    for path in datasets:
        bars = load_bars(path)
        for fold in folds(len(bars), train=train, test=test):
            train_slice = bars[fold.train_start : fold.train_end]
            test_slice = bars[fold.train_end : fold.test_end]

            scored = []
            for config in candidates:
                mean, episodes = _score(train_slice, config, cost_fraction)
                if episodes >= minimum_train_episodes:
                    scored.append((mean, config))
            if not scored:
                continue
            best_mean, best_config = max(scored, key=lambda item: item[0])

            tuned_test, tuned_episodes = _score(test_slice, best_config, cost_fraction)
            fixed_test, fixed_episodes = _score(test_slice, shipped, cost_fraction)
            if tuned_episodes == 0 and fixed_episodes == 0:
                continue

            tuned_is.append(best_mean)
            tuned_oos.append(tuned_test)
            fixed_oos.append(fixed_test)
            rows.append(
                {
                    "dataset": path.name,
                    "fold": fold.index,
                    "train_mean": round(best_mean, 8),
                    "test_mean_tuned": round(tuned_test, 8),
                    "test_mean_fixed": round(fixed_test, 8),
                    "selected_momentum": str(best_config.minimum_momentum_return),
                    "selected_volatility_cap": str(best_config.maximum_realized_volatility),
                    "test_episodes_tuned": tuned_episodes,
                }
            )

    def summarise(values: list[float]) -> dict:
        if not values:
            return {"folds": 0, "mean": None, "stdev": None, "positive_folds": 0}
        return {
            "folds": len(values),
            "mean": round(statistics.fmean(values), 8),
            "stdev": round(statistics.pstdev(values), 8) if len(values) > 1 else 0.0,
            "positive_folds": sum(1 for value in values if value > 0),
        }

    summary = {
        "train_window_bars": train,
        "test_window_bars": test,
        "grid_size": len(candidates),
        "tuned_in_sample": summarise(tuned_is),
        "tuned_out_of_sample": summarise(tuned_oos),
        "fixed_out_of_sample": summarise(fixed_oos),
        "folds": rows,
    }
    if tuned_is and tuned_oos:
        summary["overfitting_gap"] = round(
            summary["tuned_in_sample"]["mean"] - summary["tuned_out_of_sample"]["mean"], 8
        )
        summary["tuning_beat_fixed_out_of_sample"] = (
            summary["tuned_out_of_sample"]["mean"] > summary["fixed_out_of_sample"]["mean"]
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--train", type=int, default=365)
    parser.add_argument("--test", type=int, default=120)
    parser.add_argument("--cost-fraction", type=Decimal, default=Decimal("0.0016"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    summary = run(
        args.datasets, train=args.train, test=args.test, cost_fraction=args.cost_fraction
    )
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    if not summary["tuned_out_of_sample"]["folds"]:
        print("no fold carried enough episodes to score", file=sys.stderr)
        return 1

    print(
        f"train {summary['train_window_bars']} bars, test {summary['test_window_bars']} bars, "
        f"{summary['grid_size']} parameter combinations, "
        f"{summary['tuned_out_of_sample']['folds']} folds\n"
    )
    for label, key in (
        ("tuned, in sample", "tuned_in_sample"),
        ("tuned, out of sample", "tuned_out_of_sample"),
        ("shipped, out of sample", "fixed_out_of_sample"),
    ):
        block = summary[key]
        share = block["positive_folds"] / block["folds"] if block["folds"] else 0
        print(
            f"  {label:<24}{block['mean']:>+10.4%} per episode   "
            f"{block['positive_folds']}/{block['folds']} folds positive ({share:.0%})"
        )
    print(f"\n  overfitting gap:        {summary['overfitting_gap']:>+10.4%} per episode")
    verdict = (
        "tuning survived the test windows"
        if summary["tuning_beat_fixed_out_of_sample"]
        else "tuning did NOT beat the shipped configuration out of sample"
    )
    print(f"  verdict:                {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
