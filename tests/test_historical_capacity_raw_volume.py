from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tools.historical_capacity_raw_volume import (
    _percentile_95,
    analyze_historical_capacity,
    load_raw_base_volume,
)

SNAPSHOTS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


def test_lossless_raw_daily_volume_and_negative_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_D.csv"
    path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2025-01-01T00:00:00+00:00,BTCUSDT,100,101,99,100,100.7501\n",
        encoding="utf-8",
    )
    values = load_raw_base_volume(path, expected_symbol="BTCUSDT")
    assert values["2025-01-01"] == Decimal("100.7501")

    path.write_text(
        path.read_text(encoding="utf-8").replace("100.7501", "NaN"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="finite"):
        load_raw_base_volume(path, expected_symbol="BTCUSDT")


def test_duplicate_raw_daily_dates_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_D.csv"
    path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2025-01-01T00:00:00+00:00,BTCUSDT,100,101,99,100,100.1\n"
        "2025-01-01T00:00:00+00:00,BTCUSDT,100,101,99,100,100.2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_raw_base_volume(path, expected_symbol="BTCUSDT")


def test_nearest_rank_p95_uses_real_decimal_precision() -> None:
    data = [Decimal(str(i)) / Decimal("10000") for i in range(1, 21)]
    assert _percentile_95(data) == Decimal("0.0019")


def test_six_major_capacity_proxy_has_complete_fill_and_cost_identity() -> None:
    data = analyze_historical_capacity(bars_dir=SNAPSHOTS, symbols=SIX)
    assert data["evaluation_class"] == (
        "REUSED_INSPECTED_HISTORY_CAPACITY_PROXY_ONLY"
    )
    assert data["strategy_promotion_allowed"] is False
    assert data["live_trading_allowed"] is False
    assert data["actual_execution_capacity_proven"] is False
    assert data["historical_market_impact_modelled"] is False
    assert data["base_replay"]["closed_trades"] == 256
    assert data["base_replay"]["fill_count"] >= 512
    assert len(data["source_sha256"]) == 6
    assert data["raw_volume_integrity"]["fractional_raw_volume_rows"] > 0

    levels = data["hypothetical_capital_scenarios"]
    previous = None
    for amount in ("10000", "100000", "1000000", "10000000"):
        row = levels[amount]
        assert row["scaled_from_usd_10000_model_no_impact"] is True
        assert row["fill_count"] == data["base_replay"]["fill_count"]
        peak = Decimal(row["max_fraction_of_full_daily_base_volume"])
        if previous is not None:
            assert peak > previous
        previous = peak
    assert (
        Decimal(levels["1000000"]["max_fraction_of_full_daily_base_volume"])
        == Decimal(levels["10000"]["max_fraction_of_full_daily_base_volume"])
        * Decimal("100")
    )
    assert len(data["evidence_sha256"]) == 64
