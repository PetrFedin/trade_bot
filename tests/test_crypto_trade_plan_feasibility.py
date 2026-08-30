from decimal import Decimal

import pytest

from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    build_trade_plan,
)
from app.strategy.crypto_trade_plan_feasibility import (
    diagnose_crypto_trade_plan_feasibility,
    minimum_atr_fraction_for_trade_plan,
    minimum_equity_for_any_strategy_valid_trade_plan,
)


def _signal(*, atr: str, quality: str) -> CryptoSignal:
    return CryptoSignal(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        reference_price=Decimal("100"),
        momentum=Decimal("0.01"),
        atr_fraction=Decimal(atr),
        fast_ema=Decimal("101"),
        slow_ema=Decimal("100"),
        breakout_strength_atr=Decimal("0.5"),
        one_bar_atr_multiple=Decimal("0.5"),
        average_turnover_usdt=Decimal("10000000"),
        quality_score=Decimal(quality),
        decision_time="2026-08-30T00:00:00+00:00",
    )


def test_frozen_1000_equity_trade_plan_gate_is_exactly_48_bps_atr() -> None:
    config = CryptoPerpStrategyConfig()

    threshold = minimum_atr_fraction_for_trade_plan(
        equity_usdt=Decimal("1000"),
        config=config,
    )

    assert threshold == Decimal("0.0048")
    below = build_trade_plan(
        _signal(atr="0.004799", quality="100"),
        equity_usdt=Decimal("1000"),
        config=config,
    )
    at_gate = build_trade_plan(
        _signal(atr="0.0048", quality="1.10"),
        equity_usdt=Decimal("1000"),
        config=config,
    )
    assert below.eligible is False
    assert at_gate.eligible is True


def test_quality_score_does_not_change_trade_plan_feasibility_at_same_atr() -> None:
    config = CryptoPerpStrategyConfig()
    low_quality = build_trade_plan(
        _signal(atr="0.006", quality="1.10"),
        equity_usdt=Decimal("1000"),
        config=config,
    )
    extreme_quality = build_trade_plan(
        _signal(atr="0.006", quality="100"),
        equity_usdt=Decimal("1000"),
        config=config,
    )

    assert low_quality.eligible is True
    assert extreme_quality.eligible is True
    assert low_quality.plan is not None
    assert extreme_quality.plan is not None
    assert low_quality.plan.expected_net_edge_usd == extreme_quality.plan.expected_net_edge_usd


def test_fixed_dollar_target_requires_higher_atr_as_equity_falls() -> None:
    config = CryptoPerpStrategyConfig()
    expected = {
        Decimal("1000"): 0.0048,
        Decimal("950"): 0.005552941176470588,
        Decimal("900"): 0.00662857142857143,
        Decimal("850"): 0.008290909090909092,
        Decimal("800"): 0.0112,
        Decimal("750"): 0.0176,
    }

    observed = []
    for equity, value in expected.items():
        threshold = minimum_atr_fraction_for_trade_plan(
            equity_usdt=equity,
            config=config,
        )
        assert threshold is not None
        assert float(threshold) == pytest.approx(value)
        observed.append(threshold)
    assert observed == sorted(observed)


def test_strategy_has_minimum_equity_below_which_no_valid_atr_can_make_20_target() -> None:
    config = CryptoPerpStrategyConfig()

    minimum_equity = minimum_equity_for_any_strategy_valid_trade_plan(config)
    diagnostic = diagnose_crypto_trade_plan_feasibility(
        (Decimal("1000"), Decimal("750"), Decimal("700")),
        config=config,
    )

    assert float(minimum_equity) == pytest.approx(724.7956403269754)
    assert diagnostic["minimum_equity_usdt_for_any_strategy_valid_trade_plan"] == pytest.approx(
        724.7956403269754
    )
    assert diagnostic["points"][0]["strategy_valid_atr_available"] is True
    assert diagnostic["points"][1]["strategy_valid_atr_available"] is True
    assert diagnostic["points"][2]["strategy_valid_atr_available"] is False
    assert diagnostic["parameter_retuning_performed"] is False
    assert diagnostic["strategy_selection_allowed"] is False
    assert diagnostic["strategy_promotion_allowed"] is False
    assert diagnostic["trade_actionable"] is False
    assert diagnostic["demo_activation_allowed"] is False
    assert diagnostic["live_activation_allowed"] is False
    assert diagnostic["bybit_live_order_routing_allowed"] is False
