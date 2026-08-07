from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class PaperExecutionGuardError(ValueError):
    """Raised when a paper order plan violates an execution safety invariant."""


@dataclass(frozen=True)
class PaperExecutionLimits:
    maximum_quantity: Decimal
    maximum_notional: Decimal

    def validate(self) -> None:
        _positive_finite(self.maximum_quantity, "maximum_quantity")
        _positive_finite(self.maximum_notional, "maximum_notional")


def _positive_finite(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise PaperExecutionGuardError(f"{field} must be a positive finite Decimal")


def validate_paper_order_plan(
    *,
    quantity: Decimal,
    initial_limit_price: Decimal,
    replacement_limit_price: Decimal | None,
    limits: PaperExecutionLimits,
) -> Decimal:
    """Validate the entire price path before the first broker mutation.

    The returned value is the worst-case notional across submit and replacement.
    No caller should submit an order before this function succeeds.
    """

    limits.validate()
    _positive_finite(quantity, "quantity")
    _positive_finite(initial_limit_price, "initial_limit_price")
    if replacement_limit_price is not None:
        _positive_finite(replacement_limit_price, "replacement_limit_price")

    if quantity > limits.maximum_quantity:
        raise PaperExecutionGuardError("QUANTITY_LIMIT_EXCEEDED")

    prices = [initial_limit_price]
    if replacement_limit_price is not None:
        prices.append(replacement_limit_price)
    worst_price = max(prices)
    worst_notional = quantity * worst_price
    if not worst_notional.is_finite():
        raise PaperExecutionGuardError("NOTIONAL_NOT_FINITE")
    if worst_notional > limits.maximum_notional:
        raise PaperExecutionGuardError("NOTIONAL_LIMIT_EXCEEDED")
    return worst_notional
