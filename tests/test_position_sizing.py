from decimal import Decimal

from app.strategy.position_sizing import (
    RiskAwareSizingPolicy,
    size_position_from_risk,
)


def test_default_sizing_uses_full_thirty_percent_when_stop_risk_fits_budget() -> None:
    result = size_position_from_risk(
        equity=Decimal("10000"),
        realized_volatility=Decimal("0.01"),
        stop_loss_fraction=Decimal("0.02"),
    )
    assert result.target_equity_fraction == Decimal("0.30")
    assert result.target_notional == Decimal("3000.00")
    assert result.stop_risk_fraction_of_equity == Decimal("0.0060")
    assert result.stop_risk_amount == Decimal("60.0000")
    assert result.volatility_multiplier == Decimal("1")


def test_high_realized_volatility_reduces_position_and_stop_risk() -> None:
    result = size_position_from_risk(
        equity=Decimal("10000"),
        realized_volatility=Decimal("0.03"),
        stop_loss_fraction=Decimal("0.02"),
    )
    assert result.volatility_multiplier == Decimal("0.5")
    assert result.target_equity_fraction == Decimal("0.150")
    assert result.target_notional == Decimal("1500.000")
    assert result.stop_risk_amount == Decimal("30.00000")


def test_wider_stop_reduces_notional_to_preserve_same_risk_budget() -> None:
    result = size_position_from_risk(
        equity=Decimal("10000"),
        realized_volatility=Decimal("0.01"),
        stop_loss_fraction=Decimal("0.04"),
    )
    assert result.target_equity_fraction == Decimal("0.15")
    assert result.target_notional == Decimal("1500.00")
    assert result.stop_risk_fraction_of_equity == Decimal("0.0060")
    assert result.stop_risk_amount == Decimal("60.0000")


def test_custom_risk_budget_cannot_be_overridden_by_low_volatility() -> None:
    policy = RiskAwareSizingPolicy(
        risk_budget_fraction=Decimal("0.004"),
        maximum_equity_fraction=Decimal("0.50"),
        target_realized_volatility=Decimal("0.02"),
    )
    result = size_position_from_risk(
        equity=Decimal("25000"),
        realized_volatility=Decimal("0"),
        stop_loss_fraction=Decimal("0.02"),
        policy=policy,
    )
    assert result.target_equity_fraction == Decimal("0.2")
    assert result.target_notional == Decimal("5000.0")
    assert result.stop_risk_amount == Decimal("100.000")
