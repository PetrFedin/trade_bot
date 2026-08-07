from decimal import Decimal

import pytest

from app.runtime.paper_execution_guard import (
    PaperExecutionGuardError,
    PaperExecutionLimits,
    validate_paper_order_plan,
)


LIMITS = PaperExecutionLimits(
    maximum_quantity=Decimal("2"),
    maximum_notional=Decimal("1000"),
)


def test_worst_case_replacement_notional_is_checked_before_mutation() -> None:
    with pytest.raises(PaperExecutionGuardError, match="NOTIONAL_LIMIT_EXCEEDED"):
        validate_paper_order_plan(
            quantity=Decimal("1"),
            initial_limit_price=Decimal("900"),
            replacement_limit_price=Decimal("1001"),
            limits=LIMITS,
        )


def test_replacement_at_notional_limit_is_allowed() -> None:
    assert validate_paper_order_plan(
        quantity=Decimal("1"),
        initial_limit_price=Decimal("900"),
        replacement_limit_price=Decimal("1000"),
        limits=LIMITS,
    ) == Decimal("1000")


@pytest.mark.parametrize(
    "quantity,initial,replacement",
    [
        (Decimal("NaN"), Decimal("100"), None),
        (Decimal("Infinity"), Decimal("100"), None),
        (Decimal("1"), Decimal("NaN"), None),
        (Decimal("1"), Decimal("100"), Decimal("Infinity")),
        (Decimal("0"), Decimal("100"), None),
        (Decimal("1"), Decimal("0"), None),
    ],
)
def test_non_finite_or_non_positive_values_fail_closed(quantity, initial, replacement) -> None:
    with pytest.raises(PaperExecutionGuardError):
        validate_paper_order_plan(
            quantity=quantity,
            initial_limit_price=initial,
            replacement_limit_price=replacement,
            limits=LIMITS,
        )


def test_quantity_limit_is_checked() -> None:
    with pytest.raises(PaperExecutionGuardError, match="QUANTITY_LIMIT_EXCEEDED"):
        validate_paper_order_plan(
            quantity=Decimal("2.01"),
            initial_limit_price=Decimal("1"),
            replacement_limit_price=None,
            limits=LIMITS,
        )
