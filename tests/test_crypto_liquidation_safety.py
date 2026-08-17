from decimal import Decimal

from app.strategy.crypto_liquidation_safety import (
    CryptoLiquidationSafetyPolicy,
    CryptoLiquidationSafetyStatus,
    evaluate_crypto_liquidation_safety,
)
from app.strategy.crypto_perp import CryptoSide


def test_long_liquidation_must_remain_beyond_stop_with_one_r_buffer() -> None:
    safe = evaluate_crypto_liquidation_safety(
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("98"),
        liquidation_price=Decimal("96"),
    )
    thin = evaluate_crypto_liquidation_safety(
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("98"),
        liquidation_price=Decimal("97"),
    )

    assert safe.status is CryptoLiquidationSafetyStatus.SAFE
    assert safe.safe is True
    assert safe.stop_to_liquidation_buffer_r == Decimal("1")
    assert safe.demo_activation_allowed is False
    assert safe.live_activation_allowed is False
    assert thin.status is CryptoLiquidationSafetyStatus.LIQUIDATION_BUFFER_TOO_SMALL
    assert thin.safe is False
    assert thin.stop_to_liquidation_buffer_r == Decimal("0.5")


def test_short_liquidation_ordering_is_directionally_inverted() -> None:
    safe = evaluate_crypto_liquidation_safety(
        side=CryptoSide.SHORT,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("102"),
        liquidation_price=Decimal("104"),
    )

    assert safe.status is CryptoLiquidationSafetyStatus.SAFE
    assert safe.stop_to_liquidation_buffer == Decimal("2")
    assert safe.stop_to_liquidation_buffer_r == Decimal("1")


def test_liquidation_inside_hard_stop_fails_closed() -> None:
    decision = evaluate_crypto_liquidation_safety(
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("98"),
        liquidation_price=Decimal("99"),
    )

    assert decision.status is (
        CryptoLiquidationSafetyStatus.LIQUIDATION_NOT_BEYOND_HARD_STOP
    )
    assert decision.safe is False
    assert decision.stop_to_liquidation_buffer is None


def test_missing_liquidation_price_is_not_treated_as_safe() -> None:
    decision = evaluate_crypto_liquidation_safety(
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("98"),
        liquidation_price=None,
    )

    assert decision.status is CryptoLiquidationSafetyStatus.LIQUIDATION_PRICE_UNAVAILABLE
    assert decision.safe is False
    assert decision.reasons == ("LIQUIDATION_PRICE_UNAVAILABLE",)


def test_policy_can_require_larger_shadow_buffer_without_enabling_demo() -> None:
    decision = evaluate_crypto_liquidation_safety(
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("98"),
        liquidation_price=Decimal("94"),
        policy=CryptoLiquidationSafetyPolicy(
            minimum_stop_to_liquidation_buffer_r=Decimal("2")
        ),
    )

    assert decision.safe is True
    assert decision.required_buffer_r == Decimal("2")
    assert decision.shadow_only is True
    assert decision.demo_activation_allowed is False
