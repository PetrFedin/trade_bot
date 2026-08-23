from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_research_universe import BybitResearchInstrument
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_source_common_period_evidence import (
    build_source_common_period_symbol_evidence_rows,
)

_START = datetime(2026, 8, 22, tzinfo=UTC)
_END = _START + timedelta(days=1)
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
        launch_time_ms=int(_START.timestamp() * 1000),
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


def _open_interest() -> tuple[BybitOpenInterestPoint, ...]:
    return tuple(
        BybitOpenInterestPoint(
            symbol="BTCUSDT",
            timestamp_ms=int((_START + index * _STEP).timestamp() * 1000),
            open_interest=Decimal("100000") + index,
            single_open_interest=None,
        )
        for index in range(288)
    )


def _account_ratio() -> tuple[BybitAccountRatioPoint, ...]:
    return tuple(
        BybitAccountRatioPoint(
            symbol="BTCUSDT",
            timestamp_ms=int((_START + index * _STEP).timestamp() * 1000),
            buy_ratio=Decimal("0.60"),
            sell_ratio=Decimal("0.40"),
        )
        for index in range(288)
    )


def _funding() -> tuple[BybitHistoricalFundingPoint, ...]:
    return tuple(
        BybitHistoricalFundingPoint(
            symbol="BTCUSDT",
            timestamp_ms=int((_START + timedelta(hours=hour)).timestamp() * 1000),
            funding_rate=Decimal("0.0001"),
        )
        for hour in (0, 8, 16)
    )


def test_source_common_period_builds_exact_point_in_time_evidence_rows() -> None:
    rows, summary = build_source_common_period_symbol_evidence_rows(
        _instrument(),
        bars=_bars(),
        open_interest=_open_interest(),
        account_ratio=_account_ratio(),
        funding=_funding(),
        common_start_at=_START,
        end_exclusive_at=_END,
    )

    assert rows
    assert all(row.symbol == "BTCUSDT" for row in rows)
    assert all(row.crowding_regime == "LONG_HEAVY" for row in rows)
    assert all(row.prior_funding_regime == "FUNDING_POSITIVE" for row in rows)
    assert all(row.liquidation_history_available is False for row in rows)
    assert summary["price_grid_complete"] is True
    assert summary["closed_trade_count"] == len(rows)
    assert summary["complete_decision_context_count"] == len(rows)
    assert summary["strategy_parameters_changed"] is False
    assert summary["strategy_promotion_allowed"] is False
    assert summary["bybit_live_order_routing_allowed"] is False


def test_source_common_period_rejects_price_gap_and_strategy_drift() -> None:
    with pytest.raises(ValueError, match="price grid count mismatch"):
        build_source_common_period_symbol_evidence_rows(
            _instrument(),
            bars=_bars()[:-1],
            open_interest=_open_interest(),
            account_ratio=_account_ratio(),
            funding=_funding(),
            common_start_at=_START,
            end_exclusive_at=_END,
        )

    default = CryptoPerpStrategyConfig()
    custom = replace(
        default,
        maximum_atr_fraction=default.maximum_atr_fraction + Decimal("0.001"),
    )
    custom.validate()
    with pytest.raises(ValueError, match="qualified fixed strategy config"):
        build_source_common_period_symbol_evidence_rows(
            _instrument(),
            bars=_bars(),
            open_interest=_open_interest(),
            account_ratio=_account_ratio(),
            funding=_funding(),
            common_start_at=_START,
            end_exclusive_at=_END,
            strategy_config=custom,
        )
