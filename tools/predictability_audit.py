"""Test whether the bars carry exploitable structure, and of which kind.

Before asking whether a momentum strategy is implemented well, it is worth asking
whether momentum is the right family at all. A market that mean-reverts over ten bars
will punish a trend-following entry no matter how the entry is filtered, and the
resulting losses look exactly like a bad signal.

The variance ratio answers that directly. For a random walk the variance of a q-period
return is q times the variance of a one-period return, so

    VR(q) = Var(q-period) / (q * Var(1-period))

is 1. Above 1 means returns carry positive autocorrelation and trends persist, which is
the condition a momentum entry needs. Below 1 means reversal: moves are partly undone,
and a breakout entry is buying what is about to be given back.

Significance uses the Lo-MacKinlay heteroskedasticity-robust statistic, because
volatility clustering alone will move a naive variance ratio away from 1 and would
otherwise be read as structure. Under the null the statistic is standard normal.

This measures the data, not a strategy. It cannot say what to trade. It can say whether
the family currently shipped is fighting the series it is pointed at.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.replay_episodes import load_bars  # noqa: E402

DEFAULT_HORIZONS = (2, 4, 8, 16)


def log_returns(closes: list[float]) -> list[float]:
    return [
        math.log(later / earlier)
        for earlier, later in zip(closes, closes[1:], strict=False)
        if earlier > 0 and later > 0
    ]


def autocorrelation(returns: list[float], lag: int) -> float:
    """Sample autocorrelation of returns at one lag."""
    if lag <= 0 or len(returns) <= lag + 1:
        raise ValueError("lag must be positive and shorter than the series")
    mean = statistics.fmean(returns)
    numerator = sum(
        (returns[index] - mean) * (returns[index - lag] - mean)
        for index in range(lag, len(returns))
    )
    denominator = sum((value - mean) ** 2 for value in returns)
    return numerator / denominator if denominator else 0.0


def variance_ratio(returns: list[float], horizon: int) -> dict:
    """Return VR(q) with the Lo-MacKinlay heteroskedasticity-robust z statistic."""
    if horizon < 2:
        raise ValueError("horizon must be at least 2")
    n = len(returns)
    if n < horizon * 8:
        raise ValueError(f"need at least {horizon * 8} returns for horizon {horizon}")

    mean = statistics.fmean(returns)
    deviations = [value - mean for value in returns]
    sum_squares = sum(value * value for value in deviations)
    variance_one = sum_squares / (n - 1)
    if variance_one <= 0:
        raise ValueError("series has no variance")

    # Overlapping q-period sums, with the Lo-MacKinlay unbiasing denominator.
    denominator = horizon * (n - horizon + 1) * (1 - horizon / n)
    total = 0.0
    for index in range(horizon - 1, n):
        window = sum(returns[index - horizon + 1 : index + 1]) - horizon * mean
        total += window * window
    variance_q = total / denominator
    ratio = variance_q / variance_one

    # Heteroskedasticity-robust variance of the ratio.
    theta = 0.0
    for lag in range(1, horizon):
        numerator = sum(
            (deviations[index] ** 2) * (deviations[index - lag] ** 2)
            for index in range(lag, n)
        )
        delta = numerator / (sum_squares**2) if sum_squares else 0.0
        weight = 2.0 * (horizon - lag) / horizon
        theta += (weight**2) * delta

    z_score = (ratio - 1.0) / math.sqrt(theta) if theta > 0 else 0.0
    return {
        "horizon": horizon,
        "variance_ratio": round(ratio, 6),
        "z_score": round(z_score, 4),
        "regime": _regime(z_score),
    }


def _regime(z_score: float) -> str:
    if z_score > 2.58:
        return "TRENDING"
    if z_score < -2.58:
        return "MEAN_REVERTING"
    return "RANDOM_WALK"


def audit(path: Path, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> dict:
    bars = load_bars(path)
    closes = [float(bar.close) for bar in bars]
    returns = log_returns(closes)
    if len(returns) < 100:
        raise ValueError(f"{path.name}: too few returns ({len(returns)})")

    ratios = []
    for horizon in horizons:
        try:
            ratios.append(variance_ratio(returns, horizon))
        except ValueError:
            continue
    return {
        "dataset": path.name,
        "symbol": bars[0].symbol,
        "bars": len(bars),
        "sigma": round(statistics.pstdev(returns), 6),
        "autocorrelation": {
            f"lag_{lag}": round(autocorrelation(returns, lag), 6) for lag in (1, 2, 3, 5, 10)
        },
        "variance_ratios": ratios,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--horizons", nargs="+", type=int, default=list(DEFAULT_HORIZONS))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    reports = []
    for path in args.datasets:
        try:
            reports.append(audit(path, tuple(args.horizons)))
        except ValueError as error:
            print(f"skipped: {error}", file=sys.stderr)
    if not reports:
        return 1

    if args.json:
        print(json.dumps(reports, indent=2))
        return 0

    horizons = [ratio["horizon"] for ratio in reports[0]["variance_ratios"]]
    head = f"{'dataset':<20}{'ac(1)':>8}" + "".join(f"{'VR' + str(q):>9}" for q in horizons)
    print(head + f"{'verdict':>17}")
    for report in reports:
        cells = "".join(f"{ratio['variance_ratio']:>9.3f}" for ratio in report["variance_ratios"])
        regimes = [ratio["regime"] for ratio in report["variance_ratios"]]
        verdict = max(set(regimes), key=regimes.count) if regimes else "-"
        print(
            f"{report['dataset']:<20}{report['autocorrelation']['lag_1']:>8.4f}"
            f"{cells}{verdict:>17}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
