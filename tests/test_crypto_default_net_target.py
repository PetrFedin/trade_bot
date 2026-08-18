from decimal import Decimal

from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    build_trade_plan,
)


def _signal() -> CryptoSignal:
    return CryptoSignal(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        reference_price=Decimal("100000"),
        momentum=Decimal("0.01"),
        atr_fraction=Decimal("0.05"),
        fast_ema=Decimal("100100"),
        slow_ema=Decimal("100000"),
        breakout_strength_atr=Decimal("1"),
        one_bar_atr_multiple=Decimal("1"),
        average_turnover_usdt=Decimal("10000000"),
        quality_score=Decimal("2"),
        decision_time="2026-08-18T00:00:00+00:00",
    )


def test_default_crypto_strategy_never_falls_back_to_fifteen_dollar_entry_target() -> None:
    config = CryptoPerpStrategyConfig()
    assert config.target_net_profit_usd == Decimal("20")

    evaluation = build_trade_plan(
        _signal(),
        equity_usdt=Decimal("1000"),
        config=config,
    )
    assert evaluation.eligible is True
    assert evaluation.plan is not None
    assert evaluation.plan.target_net_profit_usd == Decimal("20")
    assert evaluation.plan.expected_net_edge_usd >= Decimal("20")
