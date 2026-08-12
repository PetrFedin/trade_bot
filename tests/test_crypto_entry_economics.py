from dataclasses import replace
from decimal import Decimal

from app.strategy.crypto_entry_economics import evaluate_entry_economics
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1500"),
        reference_quantity=Decimal("0.015"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("1.5"),
        estimated_stop_loss_after_cost_usdt=Decimal("7.5"),
        target_net_profit_usd=Decimal("15"),
        required_move_fraction=Decimal("0.011"),
        expected_move_fraction=Decimal("0.015"),
        expected_net_edge_usd=Decimal("20"),
        quality_score=Decimal("2.5"),
    )


def test_strong_trade_plan_passes_shadow_economics_gate() -> None:
    decision = evaluate_entry_economics(_plan())

    assert decision.eligible is True
    assert decision.expected_edge_to_target == Decimal("20") / Decimal("15")
    assert decision.round_trip_cost_to_target == Decimal("0.1")
    assert decision.target_to_risk_budget == Decimal("1.5")
    assert decision.shadow_only is True
    assert decision.demo_activation_allowed is False
    assert decision.live_activation_allowed is False


def test_thin_edge_is_rejected_even_if_base_plan_met_exact_target() -> None:
    decision = evaluate_entry_economics(
        replace(_plan(), expected_net_edge_usd=Decimal("15.5"))
    )

    assert decision.eligible is False
    assert "EXPECTED_EDGE_BUFFER_TOO_THIN" in decision.reasons


def test_high_cost_share_is_rejected() -> None:
    decision = evaluate_entry_economics(
        replace(_plan(), estimated_round_trip_cost_usdt=Decimal("3"))
    )

    assert decision.eligible is False
    assert "EXECUTION_COST_SHARE_TOO_HIGH" in decision.reasons


def test_low_target_to_risk_is_rejected_for_high_frequency_small_target() -> None:
    decision = evaluate_entry_economics(
        replace(_plan(), risk_budget_usdt=Decimal("12"))
    )

    assert decision.eligible is False
    assert "TARGET_TO_RISK_RATIO_TOO_LOW" in decision.reasons
