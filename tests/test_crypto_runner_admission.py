from decimal import Decimal

import pytest

from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_runner_admission import (
    CryptoRunnerAdmissionPolicy,
    evaluate_crypto_runner_admission,
)


def _plan(*, target: str = "20", expected: str = "30") -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1500"),
        reference_quantity=Decimal("0.015"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("2.4"),
        estimated_stop_loss_after_cost_usdt=Decimal("8.4"),
        target_net_profit_usd=Decimal(target),
        required_move_fraction=Decimal("0.015"),
        expected_move_fraction=Decimal("0.022"),
        expected_net_edge_usd=Decimal(expected),
        quality_score=Decimal("2.2"),
    )


def test_default_runner_admission_requires_30_expected_net_for_20_activation() -> None:
    admitted = evaluate_crypto_runner_admission(_plan(expected="30"))
    blocked = evaluate_crypto_runner_admission(_plan(expected="29.99"))

    assert admitted.eligible is True
    assert admitted.required_expected_net_edge_usd == Decimal("30.00")
    assert admitted.strategy_promotion_allowed is False
    assert admitted.live_activation_allowed is False
    assert blocked.eligible is False
    assert blocked.reasons == ("RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN",)


def test_runner_admission_never_uses_a_15_dollar_entry_plan() -> None:
    decision = evaluate_crypto_runner_admission(_plan(target="15", expected="50"))

    assert decision.eligible is False
    assert decision.reasons == ("RUNNER_REQUIRES_MINIMUM_20_USD_ENTRY_EDGE",)


def test_runner_admission_scales_with_activation_amount() -> None:
    decision = evaluate_crypto_runner_admission(
        _plan(target="25", expected="37.50"),
        runner_policy=CryptoProfitRunnerPolicy(
            activation_net_profit_usd=Decimal("25"),
            protected_net_profit_usd=Decimal("15"),
        ),
    )

    assert decision.eligible is True
    assert decision.required_expected_net_edge_usd == Decimal("37.50")


def test_runner_admission_policy_rejects_no_excess_buffer() -> None:
    with pytest.raises(ValueError, match="greater than 1"):
        CryptoRunnerAdmissionPolicy(minimum_expected_edge_multiple=Decimal("1")).validate()
