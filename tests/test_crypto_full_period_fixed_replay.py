from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_research_universe import BybitResearchInstrument
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_full_period_fixed_replay import (
    audit_full_period_5m_price_grid,
    qualified_fixed_strategy_contract_fingerprint,
    run_qualified_full_period_symbol_replay,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from tools.research_bybit_crypto_strategy_v2 import run_crypto_strategy_v2_suite

_LAUNCH = datetime(2026, 8, 22, 0, 3, tzinfo=UTC)
_LAST_DAY = date(2026, 8, 22)
_START = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _instrument() -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol="BTCUSDT",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=int(_LAUNCH.timestamp() * 1000),
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def _bars() -> tuple[BybitKlineBar, ...]:
    rows: list[BybitKlineBar] = []
    for index in range(288):
        start = _START + index * _STEP
        opened = Decimal("100") + Decimal(index) / Decimal("10")
        close = opened + Decimal("0.05")
        rows.append(
            BybitKlineBar(
                symbol="BTCUSDT",
                start_time=start,
                open=opened,
                high=close + Decimal("0.10"),
                low=opened - Decimal("0.10"),
                close=close,
                volume=Decimal("1000"),
                turnover=Decimal("100000"),
            )
        )
    return tuple(rows)


def test_price_grid_accepts_partial_launch_bucket_but_requires_every_5m_bucket() -> None:
    bars = _bars()
    coverage = audit_full_period_5m_price_grid(
        _instrument(),
        bars,
        last_archive_date=_LAST_DAY,
    )

    assert coverage.first_expected_bar_at == _START.isoformat()
    assert coverage.expected_bar_count == 288
    assert coverage.actual_bar_count == 288
    assert coverage.missing_bar_count == 0
    assert coverage.extra_bar_count == 0
    assert coverage.full_period_price_grid_complete is True

    missing = bars[:100] + bars[101:]
    incomplete = audit_full_period_5m_price_grid(
        _instrument(),
        missing,
        last_archive_date=_LAST_DAY,
    )
    assert incomplete.full_period_price_grid_complete is False
    assert incomplete.missing_bar_count == 1
    assert incomplete.first_missing_bar_at == bars[100].start_time.isoformat()


def test_full_period_replay_refuses_any_price_grid_gap() -> None:
    bars = _bars()
    with pytest.raises(ValueError, match="incomplete 5m price grid"):
        run_qualified_full_period_symbol_replay(
            _instrument(),
            bars[:-1],
            last_archive_date=_LAST_DAY,
        )


def test_full_period_replay_matches_qualified_conditional_1_5x_baseline() -> None:
    bars = _bars()
    result = run_qualified_full_period_symbol_replay(
        _instrument(),
        bars,
        last_archive_date=_LAST_DAY,
        opening_equity_usdt=Decimal("1000"),
    )
    acquisition = BybitKlineAcquisition(
        bars=bars,
        pages_by_symbol={"BTCUSDT": 1},
    )
    suite = run_crypto_strategy_v2_suite(
        acquisition,
        opening_equity_usdt=Decimal("1000"),
    )
    baseline = suite["candidates"]["CONDITIONAL_1_5X"]

    assert result["replay"] == baseline
    assert result["price_history_full_period"] is True
    assert result["derivatives_history_full_period"] is False
    assert result["full_period_evidence_matrix_allowed"] is False
    assert result["strategy_parameters_changed"] is False
    assert result["parameter_retuning_performed"] is False
    assert result["strategy_promotion_allowed"] is False
    assert result["demo_activation_allowed"] is False
    assert result["live_activation_allowed"] is False
    assert result["bybit_live_order_routing_allowed"] is False


def test_full_period_replay_rejects_custom_strategy_and_fingerprint_is_stable() -> None:
    first = qualified_fixed_strategy_contract_fingerprint()
    second = qualified_fixed_strategy_contract_fingerprint()
    assert first == second
    assert len(first) == 64

    default = CryptoPerpStrategyConfig()
    with pytest.raises(ValueError, match="qualified fixed strategy config"):
        run_qualified_full_period_symbol_replay(
            _instrument(),
            _bars(),
            last_archive_date=_LAST_DAY,
            strategy_config=CryptoPerpStrategyConfig(
                minimum_quality_score=default.minimum_quality_score + Decimal("0.1")
            ),
        )
