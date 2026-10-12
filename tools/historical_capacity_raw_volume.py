"""Research-only capital-size and turnover participation on frozen Bybit daily data.

Actual exchange executable capacity is NOT measured. The unchanged shipped
portfolio is replayed once at USD 10,000. Quantity requirements are scaled
linearly to hypothetical capital levels and divided by exact Decimal source
base-volume. The full DAY's traded base volume is an optimistic denominator,
not depth/availability at the fill timestamp or proof of market impact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_trade_attribution import run_shipped_portfolio

BASE_CAPITAL = Decimal("10000")
CAPITAL_LEVELS = (
    Decimal("10000"),
    Decimal("100000"),
    Decimal("1000000"),
    Decimal("10000000"),
)


def load_raw_base_volume(
    path: Path, *, expected_symbol: str
) -> dict[str, Decimal]:
    """Load raw base-asset daily volume without float or integer conversion."""
    by_day: dict[str, Decimal] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("symbol") != expected_symbol:
                raise ValueError("raw volume symbol mismatch")
            timestamp = datetime.fromisoformat(
                row["timestamp"].replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError("raw volume timestamp must be UTC")
            day = timestamp.date().isoformat()
            if day in by_day:
                raise ValueError("duplicate raw volume day")
            volume = Decimal(row["volume"])
            if not volume.is_finite() or volume < 0:
                raise ValueError("raw daily base volume must be finite and nonnegative")
            by_day[day] = volume
    if not by_day:
        raise ValueError("empty raw volume source")
    return by_day


def _percentile_95(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("no observations for percentile")
    ordered = sorted(values)
    index = (95 * len(ordered) + 99) // 100 - 1
    return ordered[index]


def _fills(result, synchronized, *, slip: Decimal):
    events = []
    for trade in result.closed_trades:
        for side, when, price in (
            ("BUY", trade.entry_time, trade.entry_execution_price),
            ("SELL", trade.exit_time, trade.exit_execution_price),
        ):
            events.append({
                "symbol": trade.symbol,
                "side": side,
                "time": when,
                "quantity": trade.quantity,
                "execution_price": price,
            })
    final_open = 0
    for symbol, quantity in sorted(result.final_quantities.items()):
        if quantity <= 0:
            continue
        trace = next(
            (
                item for item in reversed(result.decision_trace)
                if symbol in item.entered_symbols
            ),
            None,
        )
        if trace is None:
            raise ValueError("final open position lacks entry trace")
        bar = next(
            (
                bar for bar in synchronized[symbol]
                if bar.timestamp == trace.execution_time
            ),
            None,
        )
        if bar is None:
            raise ValueError("final entry bar not found")
        events.append({
            "symbol": symbol,
            "side": "BUY_OPEN_AT_VALUATION",
            "time": bar.timestamp,
            "quantity": quantity,
            "execution_price": bar.open * (Decimal("1") + slip),
        })
        final_open += 1
    if len(events) != result.fill_count:
        raise ValueError(
            "capacity event count differs from original portfolio fill count"
        )
    return events, final_open


def analyze_historical_capacity(
    *,
    bars_dir: Path,
    symbols: tuple[str, ...],
    capital_levels: tuple[Decimal, ...] = CAPITAL_LEVELS,
) -> dict[str, object]:
    if not symbols or symbols != tuple(sorted(set(symbols))):
        raise ValueError("symbols must be nonempty, unique and sorted")
    if not capital_levels or len(capital_levels) != len(set(capital_levels)):
        raise ValueError("capital scenarios must be unique and nonempty")
    if any(
        not capital.is_finite() or capital < BASE_CAPITAL
        for capital in capital_levels
    ):
        raise ValueError("capital cannot be nonfinite or below baseline")

    result, synchronized, timeline, daily_paths = run_shipped_portfolio(
        bars_dir=bars_dir,
        symbols=symbols,
        opening_cash=BASE_CAPITAL,
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
    )
    raw_volume = {
        symbol: load_raw_base_volume(
            bars_dir / f"{symbol}_D.csv",
            expected_symbol=symbol,
        )
        for symbol in symbols
    }
    original_fractional_volume_rows = sum(
        volume != volume.to_integral_value()
        for by_day in raw_volume.values()
        for volume in by_day.values()
    )
    events, final_open = _fills(
        result, synchronized, slip=Decimal("5") / Decimal("10000")
    )
    base_events: list[dict[str, object]] = []
    for event in events:
        symbol = event["symbol"]
        date = event["time"].date().isoformat()
        denominator = raw_volume[symbol].get(date)
        if denominator is None or denominator <= 0:
            raise ValueError("missing/zero source daily volume for modelled fill")
        participation = event["quantity"] / denominator
        base_events.append({
            "symbol": symbol,
            "side": event["side"],
            "execution_date": date,
            "execution_base_qty": event["quantity"],
            "execution_notional": (
                event["quantity"] * event["execution_price"]
            ),
            "raw_daily_base_volume": denominator,
            "base_daily_volume_fraction": participation,
        })

    per_capital: dict[str, dict[str, object]] = {}
    for capital in capital_levels:
        factor = capital / BASE_CAPITAL
        values = [
            event["base_daily_volume_fraction"] * factor
            for event in base_events
        ]
        peak = max(values)
        worst = base_events[values.index(peak)]
        by_symbol: dict[str, list[Decimal]] = defaultdict(list)
        for event, frac in zip(base_events, values, strict=True):
            by_symbol[event["symbol"]].append(frac)
        per_capital[str(capital)] = {
            "hypothetical_capital_usd": str(capital),
            "scaled_from_usd_10000_model_no_impact": True,
            "fill_count": len(values),
            "max_fraction_of_full_daily_base_volume": str(peak),
            "p95_fraction_of_full_daily_base_volume": str(
                _percentile_95(values)
            ),
            "median_fraction_of_full_daily_base_volume": str(
                sorted(values)[len(values) // 2]
            ),
            "fills_exceeding_0_1_percent_daily_volume": sum(
                frac > Decimal("0.001") for frac in values
            ),
            "fills_exceeding_1_percent_daily_volume": sum(
                frac > Decimal("0.01") for frac in values
            ),
            "fills_exceeding_5_percent_daily_volume": sum(
                frac > Decimal("0.05") for frac in values
            ),
            "fills_exceeding_entire_daily_volume": sum(
                frac > Decimal("1") for frac in values
            ),
            "most_concentrated_modelled_fill": {
                "symbol": worst["symbol"],
                "side": worst["side"],
                "execution_date": worst["execution_date"],
                "required_base_quantity": str(
                    worst["execution_base_qty"] * factor
                ),
                "raw_daily_base_volume": str(
                    worst["raw_daily_base_volume"]
                ),
                "fraction_of_daily_volume": str(peak),
            },
            "by_symbol": {
                symbol: {
                    "fill_count": len(by_symbol[symbol]),
                    "max_fraction_of_full_daily_base_volume": str(
                        max(by_symbol[symbol])
                    ),
                }
                for symbol in sorted(by_symbol)
            },
        }

    source_sha = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in daily_paths
    }
    fills_by_day = Counter(
        (event["symbol"], event["execution_date"])
        for event in base_events
    )
    payload: dict[str, object] = {
        "schema_version": "astra-historical-capital-participation-v1",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "evaluation_class": "REUSED_INSPECTED_HISTORY_CAPACITY_PROXY_ONLY",
        "historical_orderbook_depth_observed": False,
        "historical_market_impact_modelled": False,
        "actual_execution_capacity_proven": False,
        "no_strategy_parameter_changes": True,
        "units": {
            "fill_quantity": "base_asset",
            "daily_volume": "base_asset",
            "participation_fraction": "fill_base_qty / complete_daily_base_volume",
            "capital": "USD_hypothetical",
        },
        "source_sha256": dict(sorted(source_sha.items())),
        "symbols": list(symbols),
        "synchronized_window": {
            "start": timeline[0].isoformat(),
            "end": timeline[-1].isoformat(),
            "bars": len(timeline),
        },
        "base_replay": {
            "opening_cash": str(BASE_CAPITAL),
            "closed_trades": result.closed_trade_count,
            "fill_count": result.fill_count,
            "final_open_positions": final_open,
            "portfolio_return_pre_funding": str(result.total_return),
            "turnover_fraction_of_opening_cash": str(
                result.turnover_fraction
            ),
            "fees_paid_usd": str(result.fees_paid),
            "max_observed_exposure_fraction": str(
                result.maximum_gross_exposure_fraction_observed
            ),
            "same_or_next_bar_closed_trades": sum(
                trade.holding_bars <= 1 for trade in result.closed_trades
            ),
            "max_fills_on_one_symbol_day": max(fills_by_day.values()),
        },
        "raw_volume_integrity": {
            "decimal_base_volume_used_not_legacy_truncated_int": True,
            "fractional_raw_volume_rows": original_fractional_volume_rows,
        },
        "hypothetical_capital_scenarios": per_capital,
        "limitations": [
            "Only FULL-DAY base volume is available, not fill-time executable depth",
            "Scaling assumes identical quantities/decisions with no market impact",
            "Reported scenario participation is a volume-consumption proxy, not capacity",
            "Static 8bps taker fee + 5bps slippage do not scale with order size",
            "No historic limit order book, queue, partial fills or rejections",
            "No funding cost allocation in this capacity-only diagnostic",
            "Results use inspected history and cannot be final untouched OOS",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    report = analyze_historical_capacity(
        bars_dir=args.bars_dir,
        symbols=tuple(sorted(args.symbols)),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BASE",
        json.dumps(report["base_replay"], sort_keys=True),
    )
    for level, scenario in report["hypothetical_capital_scenarios"].items():
        print(
            "CAPACITY", level,
            "max=", scenario["max_fraction_of_full_daily_base_volume"],
            "p95=", scenario["p95_fraction_of_full_daily_base_volume"],
            "over_1pct=", scenario["fills_exceeding_1_percent_daily_volume"],
            "over_5pct=", scenario["fills_exceeding_5_percent_daily_volume"],
            "worst=", scenario["most_concentrated_modelled_fill"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
