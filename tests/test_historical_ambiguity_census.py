"""Full historical collision population, no outcome-based cherry picking."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tools.historical_ambiguity_census import (
    BROAD_21,
    SIX_MAJOR,
    census_for_universe,
)

BARS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
HOURLY = Path("research/snapshots/bybit_intraday_trade_cases_60m")


@pytest.mark.parametrize(
    ("symbols", "identity", "closed_count", "ambiguous_count"),
    [
        (SIX_MAJOR, "SIX_MAJOR_FULL", 256, 17),
        (BROAD_21, "BROAD_21_COMMON", 276, 24),
    ],
)
def test_full_preselected_population_and_source_hashes(
    symbols: tuple[str, ...],
    identity: str,
    closed_count: int,
    ambiguous_count: int,
) -> None:
    payload = census_for_universe(
        bars_dir=BARS,
        symbols=symbols,
        universe_id=identity,
        pinned_hourly_dir=HOURLY,
    )
    assert payload["original_portfolio"]["total_closed_trades"] == closed_count
    assert payload["original_portfolio"]["all_ambiguous_closed_trades"] == ambiguous_count
    assert payload["ambiguity_sample"]["cases"] == ambiguous_count
    assert len(payload["cases"]) == ambiguous_count
    assert len({row["case_id"] for row in payload["cases"]}) == ambiguous_count
    assert payload["ambiguity_sample"]["negative_original_cases"] >= 1
    assert payload["ambiguity_sample"]["positive_original_cases"] >= 1
    assert sum(payload["ambiguity_sample"]["by_symbol"].values()) == ambiguous_count
    assert len(payload["source_sha256"]) == len(symbols)
    assert payload["strategy_promotion_allowed"] is False
    assert payload["live_trading_allowed"] is False
    assert payload["strategy_changed"] is False
    assert payload["intraday_replay_performed"] is False
    assert payload["funding_attributed_per_trade"] is False
    assert len(payload["evidence_sha256"]) == 64
    for row in payload["cases"]:
        cost = Decimal(row["ledger_average_cost"])
        entry_execution = Decimal(row["entry_execution_price"])
        assert cost > entry_execution
        assert Decimal(row["daily_low"]) <= Decimal(row["effective_protective_price"])
        assert Decimal(row["daily_high"]) >= Decimal(row["take_profit_price"])
        assert Decimal(row["original_modelled_net_pnl_usd_before_funding"]).is_finite()


def test_real_four_case_source_coverage_not_claimed_for_other_cases() -> None:
    payload = census_for_universe(
        bars_dir=BARS,
        symbols=SIX_MAJOR,
        universe_id="SIX_MAJOR_FULL",
        pinned_hourly_dir=HOURLY,
    )
    rows = payload["cases"]
    with_source = [
        row for row in rows if row["prior_pinned_hourly_data_on_same_symbol_day"]
    ]
    assert len(with_source) == 4
    assert len(rows) == 17
    assert all(row["hourly_execution_order_not_yet_reconstructed_by_census"] for row in rows)


def test_universe_changes_are_rejected() -> None:
    with pytest.raises(ValueError, match="exact frozen"):
        census_for_universe(
            bars_dir=BARS,
            symbols=SIX_MAJOR[:-1],
            universe_id="SIX_MAJOR_FULL",
        )


def test_repeatability_on_existing_history() -> None:
    first = census_for_universe(
        bars_dir=BARS,
        symbols=SIX_MAJOR,
        universe_id="SIX_MAJOR_FULL",
        pinned_hourly_dir=HOURLY,
    )
    second = census_for_universe(
        bars_dir=BARS,
        symbols=SIX_MAJOR,
        universe_id="SIX_MAJOR_FULL",
        pinned_hourly_dir=HOURLY,
    )
    assert first == second
    assert first["evidence_sha256"] == second["evidence_sha256"]
