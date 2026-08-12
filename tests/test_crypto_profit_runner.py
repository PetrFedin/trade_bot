from decimal import Decimal

import pytest

from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_profit_runner import (
    CryptoProfitRunnerPolicy,
    build_crypto_profit_runner_levels,
    modeled_raw_trigger_for_net_profit,
)


def _plan(side: CryptoSide) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1200"),
        reference_quantity=Decimal("0.012"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("1.92"),
        estimated_stop_loss_after_cost_usdt=Decimal("6.72"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.018"),
        expected_move_fraction=Decimal("0.020"),
        expected_net_edge_usd=Decimal("24"),
        quality_score=Decimal("2.5"),
    )


def test_long_runner_activates_at_20_protects_15_and_has_no_profit_cap() -> None:
    levels = build_crypto_profit_runner_levels(
        _plan(CryptoSide.LONG),
        actual_average_entry_price=Decimal("100000"),
        actual_filled_quantity=Decimal("0.012"),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
    )

    assert Decimal("100000") < levels.protected_price_at_activation < levels.activation_price
    assert levels.trailing_distance == levels.activation_price - levels.protected_price_at_activation
    assert levels.activation_net_profit_usd == Decimal("20")
    assert levels.protected_net_profit_usd == Decimal("15")
    assert levels.profit_cap_net_profit_usd is None


def test_short_runner_is_directionally_inverted_and_uncapped() -> None:
    levels = build_crypto_profit_runner_levels(
        _plan(CryptoSide.SHORT),
        actual_average_entry_price=Decimal("100000"),
        actual_filled_quantity=Decimal("0.012"),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
    )

    assert levels.activation_price < levels.protected_price_at_activation < Decimal("100000")
    assert levels.trailing_distance == levels.protected_price_at_activation - levels.activation_price
    assert levels.profit_cap_net_profit_usd is None


def test_runner_net_trigger_includes_fee_and_expected_exit_slippage() -> None:
    config = CryptoPerpStrategyConfig(
        target_net_profit_usd=Decimal("20"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
    )
    trigger = modeled_raw_trigger_for_net_profit(
        side=CryptoSide.LONG,
        actual_average_entry_price=Decimal("100000"),
        actual_filled_quantity=Decimal("0.02"),
        desired_net_profit_usd=Decimal("20"),
        strategy_config=config,
    )

    assert trigger > Decimal("101000")


def test_runner_refuses_a_15_dollar_entry_plan() -> None:
    plan = _plan(CryptoSide.LONG)
    fifteen = CryptoTradePlan(
        **{**plan.__dict__, "target_net_profit_usd": Decimal("15")}
    )

    with pytest.raises(ValueError, match="admitted for at least"):
        build_crypto_profit_runner_levels(
            fifteen,
            actual_average_entry_price=Decimal("100000"),
            actual_filled_quantity=Decimal("0.012"),
            strategy_config=CryptoPerpStrategyConfig(),
        )


def test_runner_policy_rejects_false_guarantee_shape() -> None:
    with pytest.raises(ValueError, match="below activation"):
        CryptoProfitRunnerPolicy(
            activation_net_profit_usd=Decimal("20"),
            protected_net_profit_usd=Decimal("20"),
        ).validate()
