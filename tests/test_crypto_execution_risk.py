from decimal import Decimal

import pytest

from app.strategy.crypto_execution_risk import (
    CryptoExecutionRiskPolicy,
    resize_trade_plan_at_next_open,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        target_net_profit_usd=Decimal("20"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
    )


def _plan(*, target: str = "20") -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-17T10:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("10"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal("1.6"),
        estimated_stop_loss_after_cost_usdt=Decimal("11.6"),
        target_net_profit_usd=Decimal(target),
        required_move_fraction=Decimal("0.03"),
        expected_move_fraction=Decimal("0.05"),
        expected_net_edge_usd=Decimal("48"),
        quality_score=Decimal("2"),
    )


def test_next_open_gap_can_only_reduce_quantity_and_keep_risk_within_budget() -> None:
    decision = resize_trade_plan_at_next_open(
        _plan(),
        raw_next_open_price=Decimal("200"),
        strategy_config=_config(),
    )

    assert decision.eligible is True
    assert decision.resized is True
    assert decision.adjusted_plan is not None
    assert decision.adjusted_quantity < decision.original_quantity
    assert decision.adjusted_plan.reference_quantity == decision.adjusted_quantity
    assert decision.modeled_stop_loss_after_cost_usdt <= Decimal("10")
    assert decision.modeled_expected_net_edge_usd >= Decimal("20")
    assert decision.demo_activation_allowed is False
    assert decision.live_activation_allowed is False


def test_resize_blocks_trade_when_minimum_net_edge_no_longer_survives() -> None:
    decision = resize_trade_plan_at_next_open(
        _plan(target="50"),
        raw_next_open_price=Decimal("200"),
        strategy_config=_config(),
    )

    assert decision.eligible is False
    assert decision.resized is True
    assert decision.adjusted_plan is None
    assert "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET" in decision.reasons
    assert decision.modeled_stop_loss_after_cost_usdt <= Decimal("10")


def test_execution_risk_never_increases_quantity_on_favorable_gap() -> None:
    decision = resize_trade_plan_at_next_open(
        _plan(),
        raw_next_open_price=Decimal("50"),
        strategy_config=_config(),
    )

    assert decision.eligible is True
    assert decision.adjusted_plan is not None
    assert decision.adjusted_quantity <= Decimal("10")
    assert decision.adjusted_quantity == decision.original_quantity


def test_execution_risk_policy_rejects_budget_above_planned_risk() -> None:
    with pytest.raises(ValueError):
        CryptoExecutionRiskPolicy(maximum_risk_budget_multiple=Decimal("1.01")).validate()