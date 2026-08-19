from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide
from app.strategy.crypto_trade_management import (
    CryptoExitReason,
    CryptoProtectionPolicy,
    initial_protection_state,
    resolve_crypto_bar_exit,
    update_protection_after_completed_bar,
)
from tools.replay_bybit_crypto import replay_acquisition


def _bar(
    symbol: str,
    index: int,
    *,
    direction: int,
    start: datetime,
) -> BybitKlineBar:
    base = Decimal("100") if direction > 0 else Decimal("140")
    close = base + Decimal(direction) * Decimal("0.55") * Decimal(index)
    return BybitKlineBar(
        symbol=symbol,
        start_time=start + timedelta(minutes=5 * index),
        open=close - Decimal(direction) * Decimal("0.12"),
        high=close + Decimal("0.45"),
        low=close - Decimal("0.45"),
        close=close,
        volume=Decimal("10000"),
        turnover=Decimal("1500000"),
    )


def _synthetic_acquisition(count: int = 90) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, direction in (("BTCUSDT", 1), ("ETHUSDT", -1)):
        bars.extend(_bar(symbol, index, direction=direction, start=start) for index in range(count))
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1},
    )


def _replay_config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        minimum_average_turnover_usdt=Decimal("1000"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0.10"),
        maximum_one_bar_atr_multiple=Decimal("5"),
        risk_fraction_per_trade=Decimal("0.01"),
        maximum_notional_to_equity=Decimal("2"),
        expected_move_atr_multiple=Decimal("3"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        maximum_concurrent_positions=2,
    )


def test_replay_is_long_short_cost_aware_and_never_routes_orders() -> None:
    report = replay_acquisition(
        _synthetic_acquisition(),
        opening_equity_usdt=Decimal("1000"),
        targets_usd=(Decimal("1"), Decimal("25")),
        base_config=_replay_config(),
        protection_policy=CryptoProtectionPolicy(maximum_holding_bars=12),
    )

    assert report["qualification"] == "PASS_CRYPTO_HISTORICAL_REPLAY"
    assert report["source"] == "BYBIT_V5_PUBLIC_MAINNET_KLINE"
    assert report["strategy_promotion_allowed"] is False
    assert report["bybit_demo_order_writes_enabled"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["real_demo_fills"] is False
    assert report["funding_costs_modeled"] is False

    target_1 = report["variants"]["TARGET_1_USD"]
    trades = target_1["closed_trades"]
    assert trades
    assert {trade["side"] for trade in trades} == {"LONG", "SHORT"}
    assert target_1["metrics"]["fees_usdt"] > 0
    assert target_1["metrics"]["turnover_usdt"] > 0
    assert target_1["metrics"]["maximum_concurrent_positions"] <= 2
    assert target_1["no_lookahead_contract"] == (
        "completed bar decision -> next bar open execution"
    )
    for trade in trades:
        assert datetime.fromisoformat(trade["decision_time"]) < datetime.fromisoformat(
            trade["entry_time"]
        )

    target_25 = report["variants"]["TARGET_25_USD"]
    assert target_25["accepted_trade_plan_event_count"] <= target_1[
        "accepted_trade_plan_event_count"
    ]


def test_completed_bar_profit_protection_only_tightens_next_bar_stop() -> None:
    entry = Decimal("100")
    risk = Decimal("2")
    state = initial_protection_state(
        side=CryptoSide.LONG,
        entry_price=entry,
        hard_stop_price=Decimal("98"),
    )
    completed = BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99.5"),
        close=Decimal("102.5"),
        volume=Decimal("1"),
        turnover=Decimal("1000"),
    )
    policy = CryptoProtectionPolicy(
        break_even_activation_r=Decimal("0.8"),
        profit_lock_activation_r=Decimal("1.25"),
        profit_lock_r=Decimal("0.35"),
    )

    updated = update_protection_after_completed_bar(
        state,
        side=CryptoSide.LONG,
        entry_price=entry,
        risk_price_distance=risk,
        break_even_price=Decimal("100.2"),
        completed_bar=completed,
        policy=policy,
    )

    assert state.active_stop_price == Decimal("98")
    assert updated.active_stop_price == Decimal("100.7")
    assert updated.active_stop_reason is CryptoExitReason.PROFIT_PROTECTION
    assert updated.maximum_favorable_r == Decimal("1.5")


def test_same_bar_stop_target_ambiguity_is_resolved_against_strategy() -> None:
    bar = BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("97"),
        close=Decimal("101"),
        volume=Decimal("1"),
        turnover=Decimal("1000"),
    )

    resolved = resolve_crypto_bar_exit(
        side=CryptoSide.LONG,
        bar=bar,
        active_stop_price=Decimal("98"),
        active_stop_reason=CryptoExitReason.HARD_STOP,
        target_price=Decimal("102"),
    )

    assert resolved is not None
    assert resolved.reason is CryptoExitReason.HARD_STOP
    assert resolved.trigger_price == Decimal("98")
    assert resolved.ambiguous_intrabar_path is True


def test_cost_aware_risk_sizing_stays_under_budget() -> None:
    config = replace(
        _replay_config(),
        target_net_profit_usd=Decimal("1"),
    )
    report = replay_acquisition(
        _synthetic_acquisition(),
        opening_equity_usdt=Decimal("1000"),
        targets_usd=(Decimal("1"),),
        base_config=config,
    )

    metrics = report["variants"]["TARGET_1_USD"]["metrics"]
    assert metrics["risk_budget_breach_count"] == 0
