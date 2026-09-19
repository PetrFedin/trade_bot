"""Recompute episode statistics from bars, using the production strategy and exits.

The frozen record carries target_first and stop_first as numbers with no code behind
them: nothing in this repository computes those fields, and the record itself is marked
CARRIED_FORWARD_FROZEN_NEGATIVE_RESULT sourced from README.md. The fields are therefore
unverifiable, and their meaning is ambiguous in a way that decides the verdict.

"independent_target_stop_episodes" can mean either of two things, and they disagree:

* resolved under the shipped position management, where a trailing stop engages once
  the position is ahead and can close it near breakeven before the target is reached;
* resolved against the fixed take-profit and stop levels alone, independent of position
  management, which is what the word "independent" may be recording.

The driftless baseline differs sharply between them - roughly 0.33 for fixed levels
against roughly 0.15-0.28 under the trailing policy - so the same observed rate reads
as a real edge under one reading and as below a coin flip under the other. This
computes both from the same bars and reports them side by side, so the reading stops
being a guess.

Entries come from the shipped strategy signal, exits from the shipped intrabar
evaluator. A signal is acted on at the open of the following bar, never at the close
that produced it.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
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

DEFAULT_COST_FRACTION = Decimal("0.0016")

# Perpetual funding settles every eight hours. A long pays when the rate is positive,
# which is the ordinary state of these instruments, so ignoring it flatters a long-only
# strategy by the whole carry it was actually paying.
FUNDING_SETTLEMENT = timedelta(hours=8)


def load_funding(path: Path) -> list[tuple[datetime, float]]:
    """Read a funding csv written by tools/fetch_bybit_funding.py, oldest first."""
    rows: list[tuple[datetime, float]] = []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            rows.append(
                (datetime.fromisoformat(row["timestamp"]), float(row["funding_rate"]))
            )
    rows.sort(key=lambda item: item[0])
    return rows


def funding_paid(
    schedule: list[tuple[datetime, float]], opened: datetime, closed: datetime
) -> float:
    """Return the fraction of notional a long paid between two instants.

    Only settlements strictly inside the holding interval are charged: a position opened
    after a settlement did not pay it, and one closed before the next does not pay that.
    """
    if not schedule or closed <= opened:
        return 0.0
    stamps = [item[0] for item in schedule]
    start = bisect.bisect_right(stamps, opened)
    end = bisect.bisect_right(stamps, closed)
    return sum(rate for _, rate in schedule[start:end])


@dataclass(frozen=True)
class Episode:
    entry_index: int
    entry_price: Decimal
    policy_outcome: str
    policy_return: Decimal
    fixed_outcome: str
    bars_held: int


def load_bars(path: Path) -> list[OhlcvBar]:
    """Read an OHLCV csv written by tools/fetch_bybit_klines.py."""
    bars: list[OhlcvBar] = []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            bar = OhlcvBar(
                symbol=row["symbol"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=int(float(row["volume"])),
                trade_count=1,
            )
            bar.validate()
            bars.append(bar)
    bars.sort(key=lambda value: value.timestamp)
    return bars


def bar_sigma(bars: list[OhlcvBar]) -> float:
    """Standard deviation of per-bar log returns - the parameter every baseline needs."""
    import math

    returns = [
        math.log(float(later.close) / float(earlier.close))
        for earlier, later in zip(bars, bars[1:], strict=False)
        if earlier.close > 0 and later.close > 0
    ]
    return statistics.pstdev(returns) if len(returns) > 1 else 0.0


def _fixed_outcome(
    bars: list[OhlcvBar], start: int, entry: Decimal, policy: PositionManagementPolicy, span: int
) -> str:
    """Resolve the episode against fixed levels alone, ignoring position management.

    A bar whose range spans both levels is resolved to the stop, matching the
    conservative tie-break the production evaluator applies.
    """
    stop = entry * (Decimal("1") - policy.stop_loss_fraction)
    target = entry * (Decimal("1") + policy.take_profit_fraction)
    for bar in bars[start : start + span]:
        if bar.low <= stop:
            return "stop"
        if bar.high >= target:
            return "target"
    return "neither"


def replay(
    bars: list[OhlcvBar],
    *,
    policy: PositionManagementPolicy | None = None,
    strategy: RegimeAwareMomentumStrategy | None = None,
    cost_fraction: Decimal = DEFAULT_COST_FRACTION,
    random_entries: int | None = None,
    seed: int = 20260917,
    funding: list[tuple[datetime, float]] | None = None,
) -> dict:
    """Walk the bars once, entering on the shipped signal and exiting on the shipped policy.

    Passing random_entries replaces the signal with that many uniformly drawn entry
    points, leaving every exit rule untouched. A long-only strategy in a market that
    rose over the window collects that rise whether or not its entry carries
    information, so the random arm is what separates the two: a signal that does not
    beat it is contributing nothing but exposure.
    """
    policy = policy or PositionManagementPolicy()
    policy.validate()
    strategy = strategy or RegimeAwareMomentumStrategy()
    history = strategy.config.minimum_history_bars
    # Returning an empty report for an unusably short series invites reading "no
    # signals" where the truth is "not enough bars to ask the question".
    if len(bars) <= history + 1:
        raise ValueError(
            f"at least {history + 2} bars are required to replay a single episode, "
            f"received {len(bars)}"
        )

    closes = [Bar(symbol=b.symbol, timestamp=b.timestamp, close=b.close) for b in bars]
    episodes: list[Episode] = []
    eligible_signals = 0
    funding_charged = 0.0

    chosen: set[int] | None = None
    if random_entries is not None:
        import random as _random

        span = range(history, len(bars) - 1)
        rng = _random.Random(seed)
        chosen = set(rng.sample(list(span), min(random_entries, len(span))))

    index = history
    while index < len(bars) - 1:
        if chosen is not None:
            if index not in chosen:
                index += 1
                continue
        else:
            signal = strategy.signal(closes[max(0, index - history * 3) : index + 1])
            if not signal.eligible:
                index += 1
                continue
        eligible_signals += 1

        entry_index = index + 1
        entry = bars[entry_index].open
        state = IntrabarPositionState(peak_completed_price=entry)
        outcome = "neither"
        realised = Decimal("0")
        held = 0
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
                realised = (exit_price - entry) / entry - cost_fraction
                carry = funding_paid(
                    funding or [], bars[entry_index].timestamp, bars[position].timestamp
                )
                realised -= Decimal(str(carry))
                funding_charged += carry
                outcome = (
                    "target" if decision.reason == IntrabarExitReason.TAKE_PROFIT else "stop"
                )
                break
            state = decision.state
        else:
            closing = min(entry_index + policy.maximum_holding_bars - 1, len(bars) - 1)
            last = bars[closing]
            realised = (last.close - entry) / entry - cost_fraction
            carry = funding_paid(
                funding or [], bars[entry_index].timestamp, last.timestamp
            )
            realised -= Decimal(str(carry))
            funding_charged += carry

        episodes.append(
            Episode(
                entry_index=entry_index,
                entry_price=entry,
                policy_outcome=outcome,
                policy_return=realised,
                fixed_outcome=_fixed_outcome(
                    bars, entry_index, entry, policy, policy.maximum_holding_bars
                ),
                bars_held=held,
            )
        )
        index = entry_index + max(held, 1)

    def tally(attribute: str) -> dict:
        values = [getattr(episode, attribute) for episode in episodes]
        target = values.count("target")
        stop = values.count("stop")
        decided = target + stop
        return {
            "target_first": target,
            "stop_first": stop,
            "neither": values.count("neither"),
            "resolved": decided,
            "target_first_rate": round(target / decided, 6) if decided else None,
        }

    returns = [float(episode.policy_return) for episode in episodes]
    total = sum(returns)
    return {
        "symbol": bars[0].symbol if bars else None,
        "bars": len(bars),
        "first_bar": bars[0].timestamp.isoformat() if bars else None,
        "last_bar": bars[-1].timestamp.isoformat() if bars else None,
        "bar_sigma": round(bar_sigma(bars), 6),
        "eligible_signals": eligible_signals,
        "episodes": len(episodes),
        "under_shipped_policy": tally("policy_outcome"),
        "under_fixed_levels": tally("fixed_outcome"),
        "funding": {
            "applied": funding is not None,
            "settlements_available": len(funding) if funding else 0,
            "total_charged": round(funding_charged, 8),
            "mean_per_episode": (
                round(funding_charged / len(episodes), 8) if episodes else None
            ),
        },
        "realised": {
            "cost_fraction": str(cost_fraction),
            "mean_return_per_episode": round(total / len(returns), 8) if returns else None,
            "stdev_return_per_episode": (
                round(statistics.pstdev(returns), 8) if len(returns) > 1 else None
            ),
            "total_return": round(total, 6),
            "positive_episodes": sum(1 for value in returns if value > 0),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--cost-fraction", type=Decimal, default=DEFAULT_COST_FRACTION)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    reports = []
    for path in args.datasets:
        bars = load_bars(path)
        if len(bars) < 40:
            print(f"{path.name}: too few bars ({len(bars)})", file=sys.stderr)
            continue
        report = replay(bars, cost_fraction=args.cost_fraction)
        report["dataset"] = path.name
        reports.append(report)

    if args.json:
        print(json.dumps(reports, indent=2))
        return 0

    header = (
        f"{'dataset':<22}{'bars':>7}{'sigma':>8}{'episodes':>9}"
        f"{'policy tf':>11}{'fixed tf':>10}{'mean ret':>11}"
    )
    print(header)
    for report in reports:
        policy_rate = report["under_shipped_policy"]["target_first_rate"]
        fixed_rate = report["under_fixed_levels"]["target_first_rate"]
        mean = report["realised"]["mean_return_per_episode"]
        print(
            f"{report['dataset']:<22}{report['bars']:>7}{report['bar_sigma']:>8.4f}"
            f"{report['episodes']:>9}"
            f"{(policy_rate if policy_rate is not None else float('nan')):>11.4f}"
            f"{(fixed_rate if fixed_rate is not None else float('nan')):>10.4f}"
            f"{(mean if mean is not None else float('nan')):>11.4%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
