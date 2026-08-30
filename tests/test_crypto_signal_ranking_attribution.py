from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_signal_event_outcomes import audit_all_crypto_signal_events
from app.strategy.crypto_signal_ranking_attribution import attribute_crypto_portfolio_ranking
from tools.replay_bybit_crypto_runner import replay_open_ended_crypto_runner


def _bar(symbol: str, index: int, *, slope: str, start: datetime) -> BybitKlineBar:
    close = Decimal("100") + Decimal(slope) * Decimal(index)
    return BybitKlineBar(
        symbol=symbol,
        start_time=start + timedelta(minutes=5 * index),
        open=close - Decimal(slope) * Decimal("0.10"),
        high=close + Decimal("0.80"),
        low=close - Decimal("0.80"),
        close=close,
        volume=Decimal("10000"),
        turnover=Decimal("2000000"),
    )


def _acquisition(count: int = 120) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, slope in (
        ("BTCUSDT", "0.70"),
        ("ETHUSDT", "0.45"),
        ("SOLUSDT", "-0.55"),
    ):
        bars.extend(_bar(symbol, index, slope=slope, start=start) for index in range(count))
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        minimum_average_turnover_usdt=Decimal("1000"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0.10"),
        maximum_one_bar_atr_multiple=Decimal("5"),
        risk_fraction_per_trade=Decimal("0.01"),
        maximum_notional_to_equity=Decimal("2"),
        expected_move_atr_multiple=Decimal("10"),
        target_net_profit_usd=Decimal("20"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        maximum_concurrent_positions=2,
    )


def _evidence() -> tuple[BybitKlineAcquisition, dict, dict, CryptoPerpStrategyConfig]:
    acquisition = _acquisition()
    config = _config()
    replay = replay_open_ended_crypto_runner(
        acquisition,
        opening_equity_usdt=Decimal("1000"),
        base_config=config,
        interval="5",
    )
    signals = audit_all_crypto_signal_events(
        acquisition,
        strategy_config=config,
        reference_equity_usdt=Decimal("1000"),
    )
    return acquisition, replay, signals, config


def test_ranking_attribution_reconstructs_canonical_entries_before_shadow_comparison() -> None:
    acquisition, replay, signals, config = _evidence()

    report = attribute_crypto_portfolio_ranking(
        acquisition,
        replay,
        signals,
        strategy_config=config,
    )

    assert report["diagnostic"] == "BYBIT_CRYPTO_PORTFOLIO_RANKING_ATTRIBUTION_V1"
    assert report["decision_count"] > 0
    assert report["selected_slot_count"] > 0
    assert report["canonical_reconstruction_verified"] is True
    assert report["canonical_quality_first"]["selection_count"] == report["selected_slot_count"]
    assert report["economic_shadow"]["selection_count"] == report["selected_slot_count"]
    assert report["counterfactual_portfolio_pnl_claim_allowed"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["trade_actionable"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_ranking_attribution_fails_closed_when_entry_trace_is_tampered() -> None:
    acquisition, replay, signals, config = _evidence()
    tampered = deepcopy(replay)
    entry = next(event for event in tampered["decision_events"] if event["event"] == "ENTRY")
    entry["symbol"] = "FAKEUSDT"

    with pytest.raises(ValueError, match="reconstruction differs"):
        attribute_crypto_portfolio_ranking(
            acquisition,
            tampered,
            signals,
            strategy_config=config,
        )
