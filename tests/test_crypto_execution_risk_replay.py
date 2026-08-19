from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_execution_risk import CryptoExecutionRiskPolicy
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_trade_management import CryptoProtectionPolicy
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


def _acquisition(count: int = 90) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, direction in (("BTCUSDT", 1), ("ETHUSDT", -1)):
        bars.extend(_bar(symbol, index, direction=direction, start=start) for index in range(count))
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1},
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
        expected_move_atr_multiple=Decimal("3"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        maximum_concurrent_positions=2,
    )


def test_runner_replay_resizes_pending_quantity_at_next_open_without_risk_breach() -> None:
    report = replay_open_ended_crypto_runner(
        _acquisition(),
        opening_equity_usdt=Decimal("1000"),
        base_config=_config(),
        protection_policy=CryptoProtectionPolicy(maximum_holding_bars=12),
        runner_policy=CryptoProfitRunnerPolicy(
            activation_net_profit_usd=Decimal("1"),
            protected_net_profit_usd=Decimal("0.5"),
        ),
        execution_risk_policy=CryptoExecutionRiskPolicy(),
    )

    execution = report["execution_risk"]
    assert execution["enabled"] is True
    assert execution["resize_count"] > 0
    assert report["metrics"]["risk_budget_breach_count"] == 0
    assert any(
        event["event"] == "NEXT_OPEN_RISK_RESIZE"
        and event["adjusted_quantity"] < event["original_quantity"]
        for event in report["decision_events"]
    )
    assert report["strategy_promotion_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False