"""Build deterministic Historical Strategy Evidence from local Bybit OHLCV snapshots.

The tool performs no network calls. Datasets must be fetched separately and hash-locked
by the Bybit fetch tools. It runs the shipped cross-sectional shadow portfolio logic and
emits a canonical evidence payload. Missing funding data is explicit and prevents any
profitability-confirmed verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.qualification.portable_artifact_codec import canonical_json_bytes  # noqa: E402
from app.strategy.cross_sectional_portfolio import (  # noqa: E402
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector  # noqa: E402
from app.strategy.historical_strategy_evidence_v1 import (  # noqa: E402
    HistoricalBenchmarkV1,
    HistoricalCostCoverageV1,
    build_historical_strategy_evidence,
)
from app.strategy.position_management import PositionManagementPolicy  # noqa: E402
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy  # noqa: E402
from tools.historical_portfolio_funding import (  # noqa: E402
    FundingOpenPosition,
    funding_bounds_for_trades,
    load_funding_schedule,
)
from tools.replay_episodes import load_bars  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dataset_identity(paths: list[Path]) -> tuple[str, str]:
    rows = [
        {
            "name": path.name,
            "sha256": _sha256(path),
        }
        for path in sorted(paths)
    ]
    payload = canonical_json_bytes(rows)
    return (
        "bybit-linear-daily:" + hashlib.sha256(payload).hexdigest()[:24],
        hashlib.sha256(payload).hexdigest(),
    )


def _common_timestamps(paths: list[Path]):
    loaded = {}
    for path in paths:
        bars = load_bars(path)
        symbols = {bar.symbol for bar in bars}
        if len(symbols) != 1:
            raise ValueError(
                f"historical dataset must contain exactly one symbol: {path.name}"
            )
        symbol = next(iter(symbols))
        if symbol in loaded:
            raise ValueError(f"duplicate historical symbol dataset: {symbol}")
        loaded[symbol] = bars

    timestamp_sets = [
        {bar.timestamp for bar in bars}
        for bars in loaded.values()
    ]
    common = set.intersection(*timestamp_sets)
    if len(common) < 10:
        raise ValueError("insufficient synchronized historical bars")
    return loaded, tuple(sorted(common))


def _benchmarks(
    *,
    loaded,
    timeline,
    symbols: tuple[str, ...],
    first_execution_index: int,
    capital_fraction: Decimal,
) -> tuple[HistoricalBenchmarkV1, ...]:
    per_symbol: dict[str, Decimal] = {}
    for symbol in symbols:
        bars = {bar.timestamp: bar for bar in loaded[symbol]}
        first = bars[timeline[first_execution_index]].open
        last = bars[timeline[-1]].close
        per_symbol[symbol] = last / first - Decimal("1")

    equal_weight = sum(per_symbol.values(), Decimal("0")) / Decimal(
        len(per_symbol)
    )
    items = [
        HistoricalBenchmarkV1(
            benchmark_id="cash",
            total_return=Decimal("0"),
        ),
        HistoricalBenchmarkV1(
            benchmark_id="equal_weight_full_capital",
            total_return=equal_weight,
        ),
        HistoricalBenchmarkV1(
            benchmark_id="equal_weight_capital_matched",
            total_return=equal_weight * capital_fraction,
        ),
    ]
    if "BTCUSDT" in per_symbol:
        items.append(
            HistoricalBenchmarkV1(
                benchmark_id="btc_buy_hold",
                total_return=per_symbol["BTCUSDT"],
            )
        )
    return tuple(sorted(items, key=lambda item: item.benchmark_id))


def build_evidence(
    *,
    bars_dir: Path,
    symbols: tuple[str, ...],
    opening_cash: Decimal,
    fee_per_fill: Decimal,
    fee_bps_per_fill: Decimal,
    slippage_bps: Decimal,
    funding_dir: Path | None = None,
) -> dict[str, object]:
    if tuple(sorted(set(symbols))) != symbols:
        raise ValueError("symbols must be unique and canonically sorted")

    paths = [bars_dir / f"{symbol}_D.csv" for symbol in symbols]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"missing historical datasets: {missing}")

    funding_paths: list[Path] = []
    funding_schedules = None
    if funding_dir is not None:
        funding_paths = [funding_dir / f"{symbol}_funding.csv" for symbol in symbols]
        missing_funding = [str(path) for path in funding_paths if not path.is_file()]
        if missing_funding:
            raise ValueError(f"missing funding datasets: {missing_funding}")
        funding_schedules = {
            symbol: load_funding_schedule(funding_dir / f"{symbol}_funding.csv")
            for symbol in symbols
        }

    loaded, timeline = _common_timestamps(paths)
    bars = [
        bar
        for symbol in symbols
        for bar in loaded[symbol]
        if bar.timestamp in set(timeline)
    ]
    selector = CrossSectionalSelector(top_k=2)
    first_execution_index = selector.signal_config.minimum_history_bars
    policy = CrossSectionalPortfolioPolicy(
        opening_cash=opening_cash,
        fee_per_fill=fee_per_fill,
        fee_bps_per_fill=fee_bps_per_fill,
        slippage_bps=slippage_bps,
        maximum_gross_exposure_fraction=Decimal("0.60"),
        new_position_target_equity_fraction=Decimal("0.29"),
    )
    result = CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=policy,
        position_policy=PositionManagementPolicy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2
        ),
    ).run(bars)

    funding_open_positions: tuple[FundingOpenPosition, ...] = ()
    if funding_schedules is not None:
        slip = slippage_bps / Decimal("10000")
        open_items: list[FundingOpenPosition] = []
        for symbol, quantity in sorted(result.final_quantities.items()):
            if quantity <= 0:
                continue
            entry_trace = next(
                (
                    trace
                    for trace in reversed(result.decision_trace)
                    if symbol in trace.entered_symbols
                ),
                None,
            )
            if entry_trace is None:
                raise ValueError(f"open position missing entry trace: {symbol}")
            entry_bar = next(
                bar
                for bar in loaded[symbol]
                if bar.timestamp == entry_trace.execution_time
            )
            open_items.append(
                FundingOpenPosition(
                    symbol=symbol,
                    entry_time=entry_trace.execution_time,
                    valuation_end=timeline[-1] + timedelta(days=1),
                    quantity=quantity,
                    entry_execution_price=entry_bar.open * (Decimal("1") + slip),
                )
            )
        funding_open_positions = tuple(open_items)

    funding_bounds = (
        funding_bounds_for_trades(
            result.closed_trades,
            funding_schedules,
            open_positions=funding_open_positions,
        )
        if funding_schedules is not None
        else None
    )
    identity_paths = paths + funding_paths
    dataset_id, dataset_sha256 = _dataset_identity(identity_paths)
    strategy_config = {
        "selector": {
            "top_k": 2,
            "signal": {
                "fast_bars": selector.signal_config.fast_bars,
                "slow_bars": selector.signal_config.slow_bars,
                "momentum_lookback_bars": (
                    selector.signal_config.momentum_lookback_bars
                ),
                "volatility_bars": selector.signal_config.volatility_bars,
                "minimum_momentum_return": str(
                    selector.signal_config.minimum_momentum_return
                ),
                "minimum_trend_strength": str(
                    selector.signal_config.minimum_trend_strength
                ),
                "maximum_realized_volatility": str(
                    selector.signal_config.maximum_realized_volatility
                ),
            },
        },
        "portfolio": {
            "opening_cash": str(opening_cash),
            "fee_per_fill": str(fee_per_fill),
            "fee_bps_per_fill": str(fee_bps_per_fill),
            "slippage_bps": str(slippage_bps),
            "maximum_gross_exposure_fraction": "0.60",
            "new_position_target_equity_fraction": "0.29",
        },
        "position_management": {
            "stop_loss_fraction": "0.02",
            "take_profit_fraction": "0.04",
            "trailing_activation_fraction": "0.02",
            "trailing_stop_fraction": "0.015",
            "maximum_holding_bars": 10,
        },
        "reentry_confirmation": {
            "minimum_consecutive_eligible_bars": 2,
        },
        "funding_model": {
            "enabled": funding_bounds is not None,
            "method": "TIMING_BOUNDS_ENTRY_EXECUTION_NOTIONAL_V1",
            "mark_price_modelled": False,
        },
    }
    benchmarks = _benchmarks(
        loaded=loaded,
        timeline=timeline,
        symbols=symbols,
        first_execution_index=first_execution_index,
        capital_fraction=Decimal("0.60"),
    )
    evidence = build_historical_strategy_evidence(
        result=result,
        strategy_id="cross-sectional-selection-shadow-v1",
        strategy_config=strategy_config,
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha256,
        start=timeline[first_execution_index].isoformat(),
        end=timeline[-1].isoformat(),
        symbols=symbols,
        cost_coverage=HistoricalCostCoverageV1(
            fixed_fees_modelled=fee_per_fill > 0,
            proportional_fees_modelled=fee_bps_per_fill > 0,
            slippage_modelled=slippage_bps > 0,
            funding_modelled=funding_bounds is not None,
            queue_position_modelled=False,
            partial_fills_modelled=False,
        ),
        benchmarks=benchmarks,
        funding_cost_lower_bound=(
            None if funding_bounds is None else funding_bounds.lower_cost
        ),
        funding_cost_upper_bound=(
            None if funding_bounds is None else funding_bounds.upper_cost
        ),
        out_of_sample=False,
        walk_forward=False,
    )
    return evidence.payload()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, default=ROOT / "data" / "bybit")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--opening-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--fee-per-fill", type=Decimal, default=Decimal("0"))
    parser.add_argument("--fee-bps-per-fill", type=Decimal, default=Decimal("8"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--funding-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    payload = build_evidence(
        bars_dir=args.bars_dir,
        symbols=tuple(sorted(args.symbols)),
        opening_cash=args.opening_cash,
        fee_per_fill=args.fee_per_fill,
        fee_bps_per_fill=args.fee_bps_per_fill,
        slippage_bps=args.slippage_bps,
        funding_dir=args.funding_dir,
    )
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    print(encoded)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
