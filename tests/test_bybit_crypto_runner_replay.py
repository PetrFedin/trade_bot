from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_trade_management import (
    CryptoExitReason,
    CryptoProtectionPolicy,
    initial_protection_state,
    resolve_crypto_bar_exit,
    update_open_ended_runner_after_completed_bar,
)
from tools.replay_bybit_crypto_runner import replay_open_ended_crypto_runner


def _bar(symbol: str, index: int, *, direction: int, start: datetime) -> BybitKlineBar:
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


def test_completed_bar_runner_ratchets_without_a_take_profit_ceiling() -> None:
    state = initial_protection_state(
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("98"),
    )
    policy = CryptoProtectionPolicy()
    first = BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        open=Decimal("103"),
        high=Decimal("106"),
        low=Decimal("101"),
        close=Decimal("105"),
        volume=Decimal("1"),
        turnover=Decimal("1000"),
    )
    armed = update_open_ended_runner_after_completed_bar(
        state,
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        risk_price_distance=Decimal("2"),
        break_even_price=Decimal("100.2"),
        runner_activation_price=Decimal("105"),
        runner_protected_price_at_activation=Decimal("103"),
        runner_trailing_distance=Decimal("2"),
        completed_bar=first,
        policy=policy,
    )
    assert armed.active_stop_price == Decimal("104")
    assert armed.active_stop_reason is CryptoExitReason.PROFIT_PROTECTION

    second = BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, 0, 5, tzinfo=UTC),
        open=Decimal("106"),
        high=Decimal("109"),
        low=Decimal("105"),
        close=Decimal("108"),
        volume=Decimal("1"),
        turnover=Decimal("1000"),
    )
    ratcheted = update_open_ended_runner_after_completed_bar(
        armed,
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        risk_price_distance=Decimal("2"),
        break_even_price=Decimal("100.2"),
        runner_activation_price=Decimal("105"),
        runner_protected_price_at_activation=Decimal("103"),
        runner_trailing_distance=Decimal("2"),
        completed_bar=second,
        policy=policy,
    )
    assert ratcheted.active_stop_price == Decimal("107")

    no_cap_bar = BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, 0, 10, tzinfo=UTC),
        open=Decimal("110"),
        high=Decimal("150"),
        low=Decimal("109"),
        close=Decimal("145"),
        volume=Decimal("1"),
        turnover=Decimal("1000"),
    )
    assert resolve_crypto_bar_exit(
        side=CryptoSide.LONG,
        bar=no_cap_bar,
        active_stop_price=ratcheted.active_stop_price,
        active_stop_reason=ratcheted.active_stop_reason,
        target_price=None,
    ) is None


def test_stateful_runner_replay_has_no_fixed_target_exit() -> None:
    report = replay_open_ended_crypto_runner(
        _synthetic_acquisition(),
        opening_equity_usdt=Decimal("1000"),
        base_config=_replay_config(),
        protection_policy=CryptoProtectionPolicy(maximum_holding_bars=12),
        runner_policy=CryptoProfitRunnerPolicy(
            activation_net_profit_usd=Decimal("1"),
            protected_net_profit_usd=Decimal("0.5"),
        ),
    )

    assert report["mode"] == "MIN_20_NET_EDGE_OPEN_ENDED_RUNNER"
    assert report["minimum_entry_net_profit_usd"] == 1.0
    assert report["runner_activation_net_profit_usd"] == 1.0
    assert report["runner_initial_protected_net_profit_usd"] == 0.5
    assert report["profit_cap_net_profit_usd"] is None
    assert report["fixed_take_profit_enabled"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["accepted_trade_plan_event_count"] > 0
    assert report["runner_selected_trade_count"] > 0
    assert report["fixed_target_selected_trade_count"] == 0
    assert report["runner_activation_event_count"] > 0
    trades = report["closed_trades"]
    assert trades
    assert all(trade["exit_reason"] != CryptoExitReason.NET_TARGET.value for trade in trades)


def test_conditional_runner_keeps_fixed_target_when_excess_edge_gate_fails() -> None:
    report = replay_open_ended_crypto_runner(
        _synthetic_acquisition(),
        opening_equity_usdt=Decimal("1000"),
        base_config=_replay_config(),
        protection_policy=CryptoProtectionPolicy(maximum_holding_bars=12),
        runner_policy=CryptoProfitRunnerPolicy(
            activation_net_profit_usd=Decimal("1"),
            protected_net_profit_usd=Decimal("0.5"),
        ),
        runner_admission_policy=CryptoRunnerAdmissionPolicy(
            minimum_expected_edge_multiple=Decimal("1000000000")
        ),
    )

    assert report["mode"] == "MIN_20_NET_EDGE_CONDITIONAL_OPEN_ENDED_RUNNER"
    assert report["fixed_take_profit_enabled"] is True
    assert report["profit_cap_net_profit_usd"] == "CONDITIONAL_BY_TRADE"
    assert report["runner_selected_trade_count"] == 0
    assert report["fixed_target_selected_trade_count"] > 0
    assert report["runner_activation_event_count"] == 0
    assert any(
        trade["exit_reason"] == CryptoExitReason.NET_TARGET.value
        for trade in report["closed_trades"]
    )
