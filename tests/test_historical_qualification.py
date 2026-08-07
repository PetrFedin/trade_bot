from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.historical import (
    CsvHistoricalBarSource,
    HistoricalDataError,
    HistoricalDataPolicy,
)
from app.strategy.backtest import BacktestConfig
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.qualification import WalkForwardPolicy, WalkForwardQualifier
from app.strategy.regimes import HistoricalRegime, MultiRegimeQualifier

START = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)


def close_for(index: int) -> Decimal:
    if index < 30:
        return Decimal("100") + Decimal(index)
    if index < 60:
        return Decimal("160") - Decimal(index - 30)
    return Decimal("120") + Decimal(index % 5)


def write_csv(path: Path, *, changed_index: int | None = None) -> None:
    rows = ["timestamp,symbol,close"]
    for index in range(90):
        close = close_for(index)
        if changed_index == index:
            close += Decimal("0.01")
        timestamp = START + timedelta(hours=index)
        rows.append(f"{timestamp.isoformat()},AAPL,{close}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def source(path: Path) -> CsvHistoricalBarSource:
    return CsvHistoricalBarSource(
        path,
        expected_symbol="AAPL",
        policy=HistoricalDataPolicy(
            minimum_bars=60,
            maximum_gap=timedelta(hours=2),
            maximum_jump_fraction=Decimal("0.50"),
        ),
    )


def qualifier() -> MultiRegimeQualifier:
    return MultiRegimeQualifier(
        qualifier=WalkForwardQualifier(
            strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1")),
            backtest_config=BacktestConfig(
                opening_cash=Decimal("10000"),
                commission_per_fill=Decimal("0"),
                slippage_bps=Decimal("0"),
            ),
            policy=WalkForwardPolicy(
                training_bars=10,
                testing_bars=5,
                step_bars=5,
                minimum_windows=2,
                maximum_drawdown_fraction=Decimal("1"),
                minimum_mean_oos_return=Decimal("-1"),
                minimum_mean_excess_return=Decimal("-1"),
            ),
        ),
        minimum_regimes=3,
    )


def regimes() -> tuple[HistoricalRegime, ...]:
    return (
        HistoricalRegime("uptrend", START, START + timedelta(hours=30)),
        HistoricalRegime(
            "downtrend",
            START + timedelta(hours=30),
            START + timedelta(hours=60),
        ),
        HistoricalRegime(
            "range",
            START + timedelta(hours=60),
            START + timedelta(hours=90),
        ),
    )


def test_csv_dataset_has_canonical_fingerprint_and_named_regime_evidence(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    write_csv(path)
    dataset = source(path).load()
    assert dataset.symbol == "AAPL"
    assert len(dataset.bars) == 90
    assert len(dataset.canonical_sha256) == 64
    assert dataset.dataset_id.endswith(dataset.canonical_sha256[:16])

    result = qualifier().qualify(dataset, regimes())
    assert result.qualified
    assert result.dataset_id == dataset.dataset_id
    assert result.dataset_sha256 == dataset.canonical_sha256
    assert [item.regime.name for item in result.results] == ["uptrend", "downtrend", "range"]
    assert all(item.bars == 30 for item in result.results)
    assert all(item.qualification.windows for item in result.results)


def test_one_price_change_changes_dataset_identity(tmp_path: Path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    write_csv(first)
    write_csv(second, changed_index=42)
    first_dataset = source(first).load()
    second_dataset = source(second).load()
    assert first_dataset.canonical_sha256 != second_dataset.canonical_sha256
    assert first_dataset.dataset_id != second_dataset.dataset_id


def test_historical_csv_rejects_schema_naive_time_and_duplicate_time(tmp_path: Path) -> None:
    wrong_schema = tmp_path / "wrong.csv"
    wrong_schema.write_text("symbol,timestamp,close\nAAPL,2026-01-01T00:00:00Z,100\n", encoding="utf-8")
    with pytest.raises(HistoricalDataError, match="HISTORICAL_CSV_SCHEMA_MISMATCH"):
        source(wrong_schema).load()

    naive = tmp_path / "naive.csv"
    naive.write_text(
        "timestamp,symbol,close\n"
        + "\n".join(
            f"2026-01-01T{index:02d}:00:00,AAPL,{100 + index}" for index in range(20)
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HistoricalDataError, match="NAIVE_HISTORICAL_TIMESTAMP"):
        CsvHistoricalBarSource(naive).load()

    duplicate = tmp_path / "duplicate.csv"
    rows = ["timestamp,symbol,close"]
    for index in range(20):
        timestamp = START if index == 1 else START + timedelta(hours=index)
        rows.append(f"{timestamp.isoformat()},AAPL,{100 + index}")
    duplicate.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(HistoricalDataError, match="DUPLICATE_HISTORICAL_TIMESTAMP"):
        CsvHistoricalBarSource(duplicate).load()


def test_regimes_must_be_ordered_unique_and_non_overlapping(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    write_csv(path)
    dataset = source(path).load()
    bad = (
        HistoricalRegime("same", START, START + timedelta(hours=40)),
        HistoricalRegime("same", START + timedelta(hours=30), START + timedelta(hours=60)),
        HistoricalRegime("last", START + timedelta(hours=60), START + timedelta(hours=90)),
    )
    with pytest.raises(ValueError, match="unique|overlap"):
        qualifier().qualify(dataset, bad)
