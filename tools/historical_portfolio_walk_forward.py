"""Measure temporal stability of the shipped portfolio policy in rolling test windows.

This is deliberately not a parameter search. Each fold provides a fixed history/warm-up
window to the unchanged shipped selector, then starts a fresh USD portfolio exactly at
the first test bar. Test windows do not overlap. Fees, slippage, funding timing bounds
and capital-matched passive benchmarks are retained per fold.

The source history has already been inspected by prior ASTRA research. Therefore these
folds are REUSED_HISTORICAL_WALK_FORWARD_STABILITY evidence, not an untouched final
holdout and not a basis for an OOS_EDGE_CONFIRMED claim.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_portfolio_evidence import build_evidence
from tools.replay_episodes import load_bars


@dataclass(frozen=True)
class Fold:
    index: int
    context_start: int
    execution_start: int
    execution_end: int


def folds(total_bars: int, *, train_bars: int, test_bars: int) -> tuple[Fold, ...]:
    if train_bars < 60:
        raise ValueError("train_bars must be at least 60")
    if test_bars < 20:
        raise ValueError("test_bars must be at least 20")
    result: list[Fold] = []
    context_start = 0
    index = 0
    while context_start + train_bars + test_bars <= total_bars:
        result.append(
            Fold(
                index=index,
                context_start=context_start,
                execution_start=context_start + train_bars,
                execution_end=context_start + train_bars + test_bars,
            )
        )
        context_start += test_bars
        index += 1
    return tuple(result)


def _load_common(
    bars_dir: Path,
    symbols: tuple[str, ...],
    *,
    end_time: datetime | None,
):
    loaded = {}
    timestamp_sets = []
    for symbol in symbols:
        path = bars_dir / f"{symbol}_D.csv"
        if not path.is_file():
            raise ValueError(f"missing historical dataset: {path}")
        bars = load_bars(path)
        selected = [
            bar for bar in bars if end_time is None or bar.timestamp <= end_time
        ]
        if not selected:
            raise ValueError(f"no historical bars inside requested window: {symbol}")
        loaded[symbol] = selected
        timestamp_sets.append({bar.timestamp for bar in selected})
    common = set.intersection(*timestamp_sets)
    timeline = tuple(sorted(common))
    if len(timeline) < 80:
        raise ValueError("insufficient synchronized bars for walk-forward")
    return loaded, timeline


def _write_fold_csvs(
    directory: Path,
    *,
    loaded,
    symbols: tuple[str, ...],
    timeline: tuple[datetime, ...],
) -> None:
    allowed = set(timeline)
    for symbol in symbols:
        path = directory / f"{symbol}_D.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "timestamp",
                    "symbol",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ],
            )
            writer.writeheader()
            for bar in loaded[symbol]:
                if bar.timestamp not in allowed:
                    continue
                writer.writerow(
                    {
                        "timestamp": bar.timestamp.isoformat(),
                        "symbol": bar.symbol,
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": str(bar.volume),
                    }
                )


def _benchmark(payload: dict[str, object], benchmark_id: str) -> Decimal:
    for item in payload["benchmarks"]:
        if item["benchmark_id"] == benchmark_id:
            return Decimal(item["total_return"])
    raise ValueError(f"benchmark missing: {benchmark_id}")


def _mean(values: list[Decimal]) -> str | None:
    if not values:
        return None
    return str(sum(values, Decimal("0")) / Decimal(len(values)))


def _median(values: list[Decimal]) -> str | None:
    if not values:
        return None
    return str(Decimal(str(statistics.median([float(value) for value in values]))))


def run_walk_forward(
    *,
    bars_dir: Path,
    funding_dir: Path,
    symbols: tuple[str, ...],
    opening_cash: Decimal = Decimal("10000"),
    train_bars: int = 365,
    test_bars: int = 90,
    fee_bps_per_fill: Decimal = Decimal("8"),
    slippage_bps: Decimal = Decimal("5"),
    end_time: datetime | None = None,
) -> dict[str, object]:
    if tuple(sorted(set(symbols))) != symbols:
        raise ValueError("symbols must be unique and canonically sorted")
    loaded, timeline = _load_common(bars_dir, symbols, end_time=end_time)
    fold_specs = folds(len(timeline), train_bars=train_bars, test_bars=test_bars)
    if not fold_specs:
        raise ValueError("no complete walk-forward folds")

    rows: list[dict[str, object]] = []
    for fold in fold_specs:
        fold_timeline = timeline[fold.context_start : fold.execution_end]
        with tempfile.TemporaryDirectory(prefix="astra-walk-forward-") as raw:
            fold_dir = Path(raw)
            _write_fold_csvs(
                fold_dir,
                loaded=loaded,
                symbols=symbols,
                timeline=fold_timeline,
            )
            payload = build_evidence(
                bars_dir=fold_dir,
                symbols=symbols,
                opening_cash=opening_cash,
                fee_per_fill=Decimal("0"),
                fee_bps_per_fill=fee_bps_per_fill,
                slippage_bps=slippage_bps,
                funding_dir=funding_dir,
                first_execution_index=train_bars,
            )

        total_return = Decimal(payload["total_return"])
        adjusted_lower = Decimal(payload["cost_adjusted_return_lower_bound"])
        adjusted_upper = Decimal(payload["cost_adjusted_return_upper_bound"])
        matched = _benchmark(payload, "equal_weight_capital_matched")
        rows.append(
            {
                "fold": fold.index,
                "context_start": timeline[fold.context_start].isoformat(),
                "test_start": timeline[fold.execution_start].isoformat(),
                "test_end": timeline[fold.execution_end - 1].isoformat(),
                "train_bars": train_bars,
                "test_bars": test_bars,
                "pre_funding_return": str(total_return),
                "funding_adjusted_return_lower": str(adjusted_lower),
                "funding_adjusted_return_upper": str(adjusted_upper),
                "capital_matched_benchmark_return": str(matched),
                "alpha_vs_matched_lower": str(adjusted_lower - matched),
                "alpha_vs_matched_upper": str(adjusted_upper - matched),
                "max_drawdown_fraction": payload["max_drawdown_fraction"],
                "closed_trade_count": payload["closed_trade_count"],
                "win_rate": payload["win_rate"],
                "profit_factor": payload["profit_factor"],
                "turnover_fraction": payload["turnover_fraction"],
                "fees_paid": payload["fees_paid"],
                "funding_cost_lower_bound": payload["funding_cost_lower_bound"],
                "funding_cost_upper_bound": payload["funding_cost_upper_bound"],
                "verdict": payload["verdict"],
                "fold_evidence_sha256": payload["evidence_sha256"],
            }
        )

    lower = [Decimal(row["funding_adjusted_return_lower"]) for row in rows]
    upper = [Decimal(row["funding_adjusted_return_upper"]) for row in rows]
    matched = [Decimal(row["capital_matched_benchmark_return"]) for row in rows]
    pre = [Decimal(row["pre_funding_return"]) for row in rows]

    source_files = [
        bars_dir / f"{symbol}_D.csv" for symbol in symbols
    ] + [
        funding_dir / f"{symbol}_funding.csv" for symbol in symbols
    ]
    source_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_files)
    }

    payload: dict[str, object] = {
        "schema_version": "astra-portfolio-walk-forward-stability-v1",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "walk_forward_verdict": "STABILITY_ONLY_NO_PROMOTION",
        "evaluation_class": "REUSED_HISTORICAL_WALK_FORWARD_STABILITY",
        "historical_window_previously_inspected": True,
        "untouched_holdout": False,
        "out_of_sample_promotion_claim_allowed": False,
        "parameter_tuning_performed": False,
        "strategy_config_fixed": True,
        "symbols": list(symbols),
        "source_window": {
            "start": timeline[0].isoformat(),
            "end": timeline[-1].isoformat(),
            "bars": len(timeline),
        },
        "fold_policy": {
            "train_bars": train_bars,
            "test_bars": test_bars,
            "test_windows_overlap": False,
            "portfolio_restarts_each_fold": True,
            "warmup_only_before_test": True,
        },
        "cost_policy": {
            "fee_bps_per_fill": str(fee_bps_per_fill),
            "slippage_bps": str(slippage_bps),
            "funding": "TIMING_BOUNDS_ENTRY_EXECUTION_NOTIONAL_V1",
        },
        "source_sha256": source_hashes,
        "summary": {
            "folds": len(rows),
            "positive_pre_funding_folds": sum(value > 0 for value in pre),
            "positive_funding_adjusted_lower_folds": sum(value > 0 for value in lower),
            "positive_funding_adjusted_upper_folds": sum(value > 0 for value in upper),
            "beat_capital_matched_lower_folds": sum(
                value > benchmark for value, benchmark in zip(lower, matched, strict=True)
            ),
            "beat_capital_matched_upper_folds": sum(
                value > benchmark for value, benchmark in zip(upper, matched, strict=True)
            ),
            "mean_pre_funding_return": _mean(pre),
            "mean_funding_adjusted_lower": _mean(lower),
            "mean_funding_adjusted_upper": _mean(upper),
            "median_funding_adjusted_lower": _median(lower),
            "median_funding_adjusted_upper": _median(upper),
            "worst_funding_adjusted_lower": str(min(lower)),
            "best_funding_adjusted_upper": str(max(upper)),
        },
        "folds": rows,
        "interpretation_rule": (
            "This is temporal stability evidence on already inspected history. "
            "A fold may diagnose instability but cannot become an untouched holdout "
            "or grant strategy promotion."
        ),
    }
    digest_payload = dict(payload)
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(digest_payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--funding-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--opening-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--train-bars", type=int, default=365)
    parser.add_argument("--test-bars", type=int, default=90)
    parser.add_argument("--fee-bps-per-fill", type=Decimal, default=Decimal("8"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--end-time", type=datetime.fromisoformat)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    payload = run_walk_forward(
        bars_dir=args.bars_dir,
        funding_dir=args.funding_dir,
        symbols=tuple(sorted(args.symbols)),
        opening_cash=args.opening_cash,
        train_bars=args.train_bars,
        test_bars=args.test_bars,
        fee_bps_per_fill=args.fee_bps_per_fill,
        slippage_bps=args.slippage_bps,
        end_time=args.end_time,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
