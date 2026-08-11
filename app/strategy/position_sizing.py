from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskAwareSizingPolicy:
    """Transparent long-only sizing from stop risk and realized volatility.

    The policy first caps notional so a full stop loss cannot exceed the configured
    equity risk budget. It then scales that notional down when realized volatility is
    above the target regime. It never scales above the stop-risk or exposure caps.
    """

    risk_budget_fraction: Decimal = Decimal("0.006")
    maximum_equity_fraction: Decimal = Decimal("0.30")
    target_realized_volatility: Decimal = Decimal("0.015")
    volatility_floor: Decimal = Decimal("0.001")

    def validate(self) -> None:
        for name, value in (
            ("risk_budget_fraction", self.risk_budget_fraction),
            ("maximum_equity_fraction", self.maximum_equity_fraction),
            ("target_realized_volatility", self.target_realized_volatility),
            ("volatility_floor", self.volatility_floor),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.risk_budget_fraction >= 1:
            raise ValueError("risk_budget_fraction must be below one")
        if self.maximum_equity_fraction > 1:
            raise ValueError("maximum_equity_fraction cannot exceed one")
        if self.volatility_floor > self.target_realized_volatility:
            raise ValueError("volatility_floor cannot exceed target_realized_volatility")


@dataclass(frozen=True)
class PositionSizingDecision:
    target_equity_fraction: Decimal
    target_notional: Decimal
    volatility_multiplier: Decimal
    stop_risk_fraction_of_equity: Decimal
    stop_risk_amount: Decimal


def size_position_from_risk(
    *,
    equity: Decimal,
    realized_volatility: Decimal,
    stop_loss_fraction: Decimal,
    policy: RiskAwareSizingPolicy | None = None,
) -> PositionSizingDecision:
    sizing = RiskAwareSizingPolicy() if policy is None else policy
    sizing.validate()
    if not equity.is_finite() or equity <= 0:
        raise ValueError("equity must be positive and finite")
    if not realized_volatility.is_finite() or realized_volatility < 0:
        raise ValueError("realized_volatility must be finite and non-negative")
    if (
        not stop_loss_fraction.is_finite()
        or stop_loss_fraction <= 0
        or stop_loss_fraction >= 1
    ):
        raise ValueError("stop_loss_fraction must be finite and within (0, 1)")

    stop_risk_cap_fraction = min(
        sizing.maximum_equity_fraction,
        sizing.risk_budget_fraction / stop_loss_fraction,
    )
    effective_volatility = max(realized_volatility, sizing.volatility_floor)
    volatility_multiplier = min(
        Decimal("1"),
        sizing.target_realized_volatility / effective_volatility,
    )
    target_equity_fraction = stop_risk_cap_fraction * volatility_multiplier
    target_notional = equity * target_equity_fraction
    stop_risk_fraction = target_equity_fraction * stop_loss_fraction
    stop_risk_amount = equity * stop_risk_fraction

    if stop_risk_fraction > sizing.risk_budget_fraction:
        raise RuntimeError("sizing exceeded configured stop-risk budget")
    if target_equity_fraction > sizing.maximum_equity_fraction:
        raise RuntimeError("sizing exceeded configured exposure cap")

    return PositionSizingDecision(
        target_equity_fraction=target_equity_fraction,
        target_notional=target_notional,
        volatility_multiplier=volatility_multiplier,
        stop_risk_fraction_of_equity=stop_risk_fraction,
        stop_risk_amount=stop_risk_amount,
    )
