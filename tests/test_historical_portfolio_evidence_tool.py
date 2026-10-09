from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from app.strategy.historical_strategy_evidence_v1 import (
    StrategyProfitabilityVerdict,
)
from tests.test_cross_sectional_portfolio import stable_universe
from tools.historical_portfolio_evidence import build_evidence


def _write_symbol_csvs(directory: Path) -> None:
    by_symbol = {}
    for bar in stable_universe(aapl_stop_on_entry=True):
        by_symbol.setdefault(bar.symbol, []).append(bar)

    for symbol, bars in by_symbol.items():
        path = directory / f"{symbol}_D.csv"
        with path.open("w", newline="") as handle:
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
            for bar in bars:
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


def test_historical_portfolio_evidence_runner_is_deterministic(tmp_path: Path) -> None:
    _write_symbol_csvs(tmp_path)
    symbols = ("AAPL", "MSFT", "NVDA")

    first = build_evidence(
        bars_dir=tmp_path,
        symbols=symbols,
        opening_cash=Decimal("10000"),
        fee_per_fill=Decimal("0"),
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
    )
    second = build_evidence(
        bars_dir=tmp_path,
        symbols=symbols,
        opening_cash=Decimal("10000"),
        fee_per_fill=Decimal("0"),
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
    )

    assert first == second
    assert first["symbols"] == list(symbols)
    assert first["cost_coverage"]["proportional_fees_modelled"] is True
    assert first["cost_coverage"]["slippage_modelled"] is True
    assert first["cost_coverage"]["funding_modelled"] is False
    assert first["verdict"] == StrategyProfitabilityVerdict.PROFITABILITY_NOT_PROVEN.value
    assert first["evidence_sha256"] == second["evidence_sha256"]


def test_explicit_first_execution_index_preserves_warmup_only_history(
    tmp_path: Path,
) -> None:
    _write_symbol_csvs(tmp_path)
    symbols = ("AAPL", "MSFT", "NVDA")
    timeline = sorted({bar.timestamp for bar in stable_universe()})
    first_execution_index = len(timeline) - 2

    evidence = build_evidence(
        bars_dir=tmp_path,
        symbols=symbols,
        opening_cash=Decimal("10000"),
        fee_per_fill=Decimal("0"),
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
        first_execution_index=first_execution_index,
    )

    assert evidence["start"] == timeline[first_execution_index].isoformat()
