from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.strategy.crypto_perp import CryptoSide


class CryptoLiquidationSafetyStatus(StrEnum):
    SAFE = "SAFE"
    LIQUIDATION_PRICE_UNAVAILABLE = "LIQUIDATION_PRICE_UNAVAILABLE"
    LIQUIDATION_NOT_BEYOND_HARD_STOP = "LIQUIDATION_NOT_BEYOND_HARD_STOP"
    LIQUIDATION_BUFFER_TOO_SMALL = "LIQUIDATION_BUFFER_TOO_SMALL"


@dataclass(frozen=True)
class CryptoLiquidationSafetyPolicy:
    minimum_stop_to_liquidation_buffer_r: Decimal = Decimal("1.0")

    def validate(self) -> None:
        if (
            not self.minimum_stop_to_liquidation_buffer_r.is_finite()
            or self.minimum_stop_to_liquidation_buffer_r < 0
        ):
            raise ValueError("liquidation buffer R must be finite and non-negative")


@dataclass(frozen=True)
class CryptoLiquidationSafetyDecision:
    status: CryptoLiquidationSafetyStatus
    safe: bool
    side: CryptoSide
    entry_price: Decimal
    hard_stop_price: Decimal
    liquidation_price: Decimal | None
    initial_risk_price_distance: Decimal
    stop_to_liquidation_buffer: Decimal | None
    stop_to_liquidation_buffer_r: Decimal | None
    required_buffer_r: Decimal
    reasons: tuple[str, ...]
    shadow_only: bool = True
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False

    @property
    def reason(self) -> CryptoLiquidationSafetyStatus:
        """Compatibility alias for consumers that need the canonical status."""

        return self.status

    @property
    def stop_to_liquidation_r(self) -> Decimal | None:
        """Compatibility alias for the canonical stop-to-liquidation buffer in R."""

        return self.stop_to_liquidation_buffer_r


def evaluate_crypto_liquidation_safety(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    hard_stop_price: Decimal,
    liquidation_price: Decimal | None,
    policy: CryptoLiquidationSafetyPolicy | None = None,
) -> CryptoLiquidationSafetyDecision:
    """Require liquidation to remain beyond the stop with an additional safety buffer."""

    active = CryptoLiquidationSafetyPolicy() if policy is None else policy
    active.validate()
    if entry_price <= 0 or hard_stop_price <= 0:
        raise ValueError("liquidation safety entry and hard-stop prices must be positive")
    risk_distance = abs(entry_price - hard_stop_price)
    if risk_distance <= 0:
        raise ValueError("liquidation safety initial risk distance must be positive")

    if liquidation_price is None or liquidation_price <= 0:
        return _decision(
            CryptoLiquidationSafetyStatus.LIQUIDATION_PRICE_UNAVAILABLE,
            side=side,
            entry_price=entry_price,
            hard_stop_price=hard_stop_price,
            liquidation_price=liquidation_price,
            risk_distance=risk_distance,
            buffer=None,
            buffer_r=None,
            required_buffer_r=active.minimum_stop_to_liquidation_buffer_r,
            reasons=("LIQUIDATION_PRICE_UNAVAILABLE",),
        )

    if side is CryptoSide.LONG:
        valid_ordering = liquidation_price < hard_stop_price < entry_price
        buffer = hard_stop_price - liquidation_price if valid_ordering else None
    else:
        valid_ordering = entry_price < hard_stop_price < liquidation_price
        buffer = liquidation_price - hard_stop_price if valid_ordering else None
    if not valid_ordering or buffer is None:
        return _decision(
            CryptoLiquidationSafetyStatus.LIQUIDATION_NOT_BEYOND_HARD_STOP,
            side=side,
            entry_price=entry_price,
            hard_stop_price=hard_stop_price,
            liquidation_price=liquidation_price,
            risk_distance=risk_distance,
            buffer=None,
            buffer_r=None,
            required_buffer_r=active.minimum_stop_to_liquidation_buffer_r,
            reasons=("LIQUIDATION_NOT_BEYOND_HARD_STOP",),
        )

    buffer_r = buffer / risk_distance
    if buffer_r < active.minimum_stop_to_liquidation_buffer_r:
        return _decision(
            CryptoLiquidationSafetyStatus.LIQUIDATION_BUFFER_TOO_SMALL,
            side=side,
            entry_price=entry_price,
            hard_stop_price=hard_stop_price,
            liquidation_price=liquidation_price,
            risk_distance=risk_distance,
            buffer=buffer,
            buffer_r=buffer_r,
            required_buffer_r=active.minimum_stop_to_liquidation_buffer_r,
            reasons=("LIQUIDATION_BUFFER_TOO_SMALL",),
        )

    return _decision(
        CryptoLiquidationSafetyStatus.SAFE,
        side=side,
        entry_price=entry_price,
        hard_stop_price=hard_stop_price,
        liquidation_price=liquidation_price,
        risk_distance=risk_distance,
        buffer=buffer,
        buffer_r=buffer_r,
        required_buffer_r=active.minimum_stop_to_liquidation_buffer_r,
        reasons=(),
    )


def _decision(
    status: CryptoLiquidationSafetyStatus,
    *,
    side: CryptoSide,
    entry_price: Decimal,
    hard_stop_price: Decimal,
    liquidation_price: Decimal | None,
    risk_distance: Decimal,
    buffer: Decimal | None,
    buffer_r: Decimal | None,
    required_buffer_r: Decimal,
    reasons: tuple[str, ...],
) -> CryptoLiquidationSafetyDecision:
    return CryptoLiquidationSafetyDecision(
        status=status,
        safe=status is CryptoLiquidationSafetyStatus.SAFE,
        side=side,
        entry_price=entry_price,
        hard_stop_price=hard_stop_price,
        liquidation_price=liquidation_price,
        initial_risk_price_distance=risk_distance,
        stop_to_liquidation_buffer=buffer,
        stop_to_liquidation_buffer_r=buffer_r,
        required_buffer_r=required_buffer_r,
        reasons=reasons,
        shadow_only=True,
        demo_activation_allowed=False,
        live_activation_allowed=False,
    )
