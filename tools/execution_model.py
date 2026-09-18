"""Price the difference between crossing the spread and resting on it.

Every measurement in this research charged a taker round trip of 0.16%. The codebase
does not obviously trade that way: limit orders outnumber market orders in the execution
path by more than ten to one, and a resting order pays the maker side, roughly 0.04% for
the round trip. Against a measured alpha of about 0.65% per episode that difference is
not a detail - it is most of the answer.

It is also not free, and the reason is what this models. A resting bid only fills when
price comes down to it, which is disproportionately when price is falling. Taking the
cheaper fee means accepting the trades nobody wanted and missing the ones that ran away.
Quoting a maker fee against a taker fill rate would be a straightforwardly false
accounting, so the fill is simulated from the same bars: a bid at a stated offset below
the reference fills only if the next bar actually traded there.

Three arms are reported. The taker arm crosses and always fills. The maker arm rests and
sometimes does not. The unfilled arm records what the resting order missed, which is the
honest price of the cheaper fee.
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
from app.strategy.ohlcv_exit import (  # noqa: E402
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import PositionManagementPolicy  # noqa: E402
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy  # noqa: E402
from tools.replay_episodes import load_bars  # noqa: E402

# Bybit linear perpetual fees: 0.055% taker per side, 0.02% maker per side.
TAKER_ROUND_TRIP = 0.0016
MAKER_ROUND_TRIP = 0.0004
DEFAULT_OFFSET = 0.002


@dataclass(frozen=True)
class Outcome:
    filled: bool
    realised: float
    missed: float


def _run_exit(
    bars, start: int, entry: float, policy: PositionManagementPolicy
) -> float:
    """Return the gross fractional result of holding from entry under the shipped exits."""
    price = Decimal(str(round(entry, 8)))
    state = IntrabarPositionState(peak_completed_price=price)
    last_close = entry
    for offset in range(policy.maximum_holding_bars):
        index = start + offset
        if index >= len(bars):
            break
        decision = evaluate_long_intrabar_exit(
            average_cost=price, bar=bars[index], state=state, policy=policy
        )
        if decision.exit_now:
            exit_price = decision.exit_price_before_costs or bars[index].close
            _ = IntrabarExitReason  # exit reason is not needed for the accounting here
            return (float(exit_price) - entry) / entry
        state = decision.state
        last_close = float(bars[index].close)
    return (last_close - entry) / entry


def simulate(
    bars,
    *,
    offset: float = DEFAULT_OFFSET,
    policy: PositionManagementPolicy | None = None,
    strategy: RegimeAwareMomentumStrategy | None = None,
) -> dict:
    """Compare crossing the spread against resting below it on the same signals."""
    if not 0 <= offset < 0.2:
        raise ValueError("offset must be within [0, 0.2)")
    policy = policy or PositionManagementPolicy()
    strategy = strategy or RegimeAwareMomentumStrategy()
    history = strategy.config.minimum_history_bars
    closes = [Bar(symbol=b.symbol, timestamp=b.timestamp, close=b.close) for b in bars]

    taker: list[float] = []
    maker: list[float] = []
    missed: list[float] = []
    signals = 0

    index = history
    while index < len(bars) - 1:
        try:
            signal = strategy.signal(closes[max(0, index - history * 3) : index + 1])
        except ValueError:
            index += 1
            continue
        if not signal.eligible:
            index += 1
            continue
        signals += 1

        reference = float(bars[index].close)
        next_bar = bars[index + 1]

        # Taker: cross at the open of the next bar, always filled.
        taker_entry = float(next_bar.open)
        taker_gross = _run_exit(bars, index + 1, taker_entry, policy)
        taker.append(taker_gross - TAKER_ROUND_TRIP)

        # Maker: rest a bid below the reference. It fills only if the bar traded there.
        limit = reference * (1 - offset)
        if float(next_bar.low) <= limit:
            maker_gross = _run_exit(bars, index + 1, limit, policy)
            maker.append(maker_gross - MAKER_ROUND_TRIP)
        else:
            # Nothing traded: no fee, no position, and the move was missed.
            missed.append(taker_gross - TAKER_ROUND_TRIP)

        index += max(1, policy.maximum_holding_bars // 2)

    def summarise(values: list[float]) -> dict:
        if len(values) < 2:
            return {"episodes": len(values), "mean": None, "t": None}
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values)
        return {
            "episodes": len(values),
            "mean": round(mean, 8),
            "stdev": round(stdev, 8),
            "t": round(mean / (stdev / math.sqrt(len(values))), 4) if stdev else None,
        }

    attempted = len(maker) + len(missed)
    return {
        "symbol": bars[0].symbol if bars else None,
        "limit_offset": offset,
        "signals": signals,
        "fill_rate": round(len(maker) / attempted, 4) if attempted else None,
        "taker_always_filled": summarise(taker),
        "maker_when_filled": summarise(maker),
        "missed_by_resting": summarise(missed),
    }


def combine(reports: list[dict]) -> dict:
    """Pool per-symbol results, weighting by episode count."""

    def pooled(key: str) -> dict:
        total = sum(r[key]["episodes"] for r in reports if r[key]["mean"] is not None)
        if total < 2:
            return {"episodes": total, "mean": None, "t": None}
        mean = (
            sum(r[key]["mean"] * r[key]["episodes"] for r in reports if r[key]["mean"] is not None)
            / total
        )
        variance = (
            sum(
                (r[key]["stdev"] ** 2) * r[key]["episodes"]
                for r in reports
                if r[key]["mean"] is not None
            )
            / total
        )
        return {
            "episodes": total,
            "mean": round(mean, 8),
            "t": round(mean / ((variance**0.5) / math.sqrt(total)), 4) if variance > 0 else None,
        }

    fills = [r["fill_rate"] for r in reports if r["fill_rate"] is not None]
    return {
        "symbols": len(reports),
        "mean_fill_rate": round(statistics.fmean(fills), 4) if fills else None,
        "taker_always_filled": pooled("taker_always_filled"),
        "maker_when_filled": pooled("maker_when_filled"),
        "missed_by_resting": pooled("missed_by_resting"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--offset", type=float, default=DEFAULT_OFFSET)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    reports = []
    for path in args.datasets:
        bars = load_bars(path)
        if len(bars) < 60:
            continue
        reports.append(simulate(bars, offset=args.offset))
    if not reports:
        return 1

    pooled = combine(reports)
    pooled["limit_offset"] = args.offset
    if args.json:
        print(json.dumps({"pooled": pooled, "per_symbol": reports}, indent=2))
        return 0

    print(
        f"{pooled['symbols']} symbols, limit {args.offset:.2%} below the reference close, "
        f"taker {TAKER_ROUND_TRIP:.2%} / maker {MAKER_ROUND_TRIP:.2%} round trip\n"
    )
    print(f"  fill rate of the resting bid: {pooled['mean_fill_rate']:.1%}\n")
    print(f"  {'arm':<28}{'episodes':>10}{'mean':>11}{'t':>9}")
    for label, key in (
        ("taker, always filled", "taker_always_filled"),
        ("maker, when filled", "maker_when_filled"),
        ("missed by resting", "missed_by_resting"),
    ):
        block = pooled[key]
        if block["mean"] is None:
            print(f"  {label:<28}{block['episodes']:>10}{'-':>11}{'-':>9}")
            continue
        print(
            f"  {label:<28}{block['episodes']:>10}{block['mean']:>+11.4%}"
            f"{(block['t'] or 0):>+9.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
