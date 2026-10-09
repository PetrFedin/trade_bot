from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.historical_portfolio_walk_forward import folds, run_walk_forward

SYMBOLS = ("AAPL", "MSFT", "NVDA")


def _write_fixture(root: Path) -> tuple[Path, Path]:
    bars_dir = root / "bars"
    funding_dir = root / "funding"
    bars_dir.mkdir()
    funding_dir.mkdir()
    start = datetime(2026, 1, 1, tzinfo=UTC)

    for symbol_index, symbol in enumerate(SYMBOLS):
        with (bars_dir / f"{symbol}_D.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
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
            for index in range(100):
                base = Decimal("100") + Decimal(symbol_index * 3)
                if symbol == "AAPL":
                    close = base + Decimal(index) * Decimal("0.40")
                elif symbol == "MSFT":
                    close = base + Decimal(index) * Decimal("0.20")
                else:
                    close = base - Decimal(index) * Decimal("0.10")
                open_price = close - Decimal("0.05")
                writer.writerow(
                    {
                        "timestamp": (start + timedelta(days=index)).isoformat(),
                        "symbol": symbol,
                        "open": str(open_price),
                        "high": str(close + Decimal("1.00")),
                        "low": str(open_price - Decimal("1.00")),
                        "close": str(close),
                        "volume": "100000",
                    }
                )

        with (funding_dir / f"{symbol}_funding.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["timestamp", "symbol", "funding_rate"],
            )
            writer.writeheader()
            for index in range(100):
                writer.writerow(
                    {
                        "timestamp": (start + timedelta(days=index, hours=8)).isoformat(),
                        "symbol": symbol,
                        "funding_rate": "0.0001",
                    }
                )

    return bars_dir, funding_dir


def test_fold_windows_are_non_overlapping_and_predeclared() -> None:
    six = folds(1096, train_bars=365, test_bars=90)
    broad = folds(981, train_bars=365, test_bars=90)

    assert len(six) == 8
    assert len(broad) == 6
    assert all(
        left.execution_end == right.execution_start
        for left, right in zip(six, six[1:], strict=False)
    )
    assert six[0].execution_start == 365
    assert six[-1].execution_end == 1085


def test_walk_forward_is_deterministic_and_cannot_claim_untouched_oos(
    tmp_path: Path,
) -> None:
    bars_dir, funding_dir = _write_fixture(tmp_path)

    first = run_walk_forward(
        bars_dir=bars_dir,
        funding_dir=funding_dir,
        symbols=SYMBOLS,
        train_bars=60,
        test_bars=20,
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
    )
    second = run_walk_forward(
        bars_dir=bars_dir,
        funding_dir=funding_dir,
        symbols=SYMBOLS,
        train_bars=60,
        test_bars=20,
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
    )

    assert first == second
    assert first["summary"]["folds"] == 2
    assert first["research_only"] is True
    assert first["strategy_promotion_allowed"] is False
    assert first["walk_forward_verdict"] == "STABILITY_ONLY_NO_PROMOTION"
    assert first["historical_window_previously_inspected"] is True
    assert first["untouched_holdout"] is False
    assert first["out_of_sample_promotion_claim_allowed"] is False
    assert first["parameter_tuning_performed"] is False
    assert first["strategy_config_fixed"] is True
    assert all(row["verdict"] for row in first["folds"])
    assert len(first["evidence_sha256"]) == 64
