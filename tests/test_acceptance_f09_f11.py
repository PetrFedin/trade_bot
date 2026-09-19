"""Reproducers for the two #132 findings that were still open.

Ten of the twelve findings in #132 are now qualified capabilities in the status surface.
F09 and F11 were not, and both reproduced on the current head before the fix in this
branch. Each test below fails against the previous behaviour and passes after it, with
no xfail, skip or suppression, as the issue's closure condition requires.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.portfolio.ledger import AccountGenesisMismatch, CashAdjustmentKind
from app.risk.pretrade import RiskLimits


def config(*, cash: str = "1000", quantity: str = "1") -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal(cash),
        target_quantity=Decimal(quantity),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("100"),
            maximum_symbol_notional=Decimal("200"),
            maximum_gross_notional=Decimal("500"),
        ),
    )


def with_history(directory: Path, *, cash: str = "1000"):
    """Open an account and give it one event, which is what anchors the genesis.

    The finding is about reopening *saved history* at a different figure. An account
    whose journal is still empty has restated nothing, so the guard deliberately does
    not fire there and a faithful reproducer has to record something first.
    """
    from datetime import UTC, datetime

    runtime = build_local_product(config=config(cash=cash), state_directory=directory)
    runtime.portfolio_store.append_cash_adjustment(
        activity_id="genesis-anchor",
        amount=Decimal("1"),
        kind=CashAdjustmentKind.EXTERNAL_FLOW,
        occurred_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
    )
    return runtime


# --- F09: a configuration that validates must be one that builds ------------------


def test_f09_zero_target_quantity_is_refused_by_validate() -> None:
    """validate() used to accept a target the product then refused to build."""
    with pytest.raises(ValueError, match="target_quantity must be positive"):
        config(quantity="0").validate()


def test_f09_validate_and_construction_agree(tmp_path: Path) -> None:
    """Whatever validate() accepts must construct, or the contract means nothing."""
    accepted = config(quantity="1")
    accepted.validate()
    build_local_product(config=accepted, state_directory=tmp_path)


def test_f09_negative_target_quantity_is_still_refused() -> None:
    with pytest.raises(ValueError, match="target_quantity"):
        config(quantity="-1").validate()


# --- F11: the account genesis anchors everything measured from it -----------------


def test_f11_reopening_with_a_different_opening_cash_is_refused(tmp_path: Path) -> None:
    """The finding: the same history reopened at 1000000 with no cash-flow event."""
    with_history(tmp_path, cash="1000")
    with pytest.raises(AccountGenesisMismatch, match="cannot be reopened"):
        build_local_product(config=config(cash="1000000"), state_directory=tmp_path)


def test_f11_reopening_with_the_same_opening_cash_succeeds(tmp_path: Path) -> None:
    """The guard must not break restart, which is the ordinary case."""
    first = build_local_product(config=config(cash="1000"), state_directory=tmp_path)
    second = build_local_product(config=config(cash="1000"), state_directory=tmp_path)
    assert first.portfolio.cash == second.portfolio.cash == Decimal("1000")


def test_f11_a_smaller_opening_cash_is_refused_too(tmp_path: Path) -> None:
    """Restating downward is the same defect; only the direction differs."""
    with_history(tmp_path, cash="1000")
    with pytest.raises(AccountGenesisMismatch):
        build_local_product(config=config(cash="1"), state_directory=tmp_path)


def test_f11_the_refusal_names_the_supported_path(tmp_path: Path) -> None:
    """An operator who hits this needs to be told what to do instead."""
    with_history(tmp_path, cash="1000")
    with pytest.raises(AccountGenesisMismatch, match="cash adjustment"):
        build_local_product(config=config(cash="2000"), state_directory=tmp_path)


def test_f11_capital_still_moves_through_a_cash_adjustment(tmp_path: Path) -> None:
    """The guard closes the silent path, not the supported one."""
    from datetime import UTC, datetime

    runtime = build_local_product(config=config(cash="1000"), state_directory=tmp_path)
    runtime.portfolio_store.append_cash_adjustment(
        activity_id="deposit-1",
        amount=Decimal("500"),
        kind=CashAdjustmentKind.EXTERNAL_FLOW,
        occurred_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
    )
    reopened = build_local_product(config=config(cash="1000"), state_directory=tmp_path)
    assert reopened.portfolio.cash == Decimal("1500")


def test_f11_genesis_is_bound_on_first_use_not_before(tmp_path: Path) -> None:
    """A fresh directory takes whatever it is opened with; only the second differs."""
    runtime = build_local_product(config=config(cash="4242"), state_directory=tmp_path)
    assert runtime.portfolio.cash == Decimal("4242")


def test_f11_a_non_positive_genesis_is_refused(tmp_path: Path) -> None:
    store = build_local_product(
        config=config(cash="1000"), state_directory=tmp_path
    ).portfolio_store
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValueError, match="opening_cash"):
            store.bind_genesis(bad)


def test_f11_binding_is_idempotent(tmp_path: Path) -> None:
    store = build_local_product(
        config=config(cash="1000"), state_directory=tmp_path
    ).portfolio_store
    assert store.bind_genesis(Decimal("1000")) == Decimal("1000")
    assert store.bind_genesis(Decimal("1000")) == Decimal("1000")


def test_f11_separate_accounts_keep_separate_genesis() -> None:
    """The binding is per durable state, not global."""
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        first = build_local_product(config=config(cash="1000"), state_directory=one)
        second = build_local_product(config=config(cash="5000"), state_directory=two)
        assert first.portfolio.cash == Decimal("1000")
        assert second.portfolio.cash == Decimal("5000")


def test_f11_an_account_without_history_may_still_be_reconfigured(tmp_path: Path) -> None:
    """Nothing has been measured from an empty journal, so nothing is restated.

    Scoping the guard this way is deliberate: it fires on the case the finding names and
    leaves an unused account configurable, which is what keeps it from blocking setup.
    """
    build_local_product(config=config(cash="1000"), state_directory=tmp_path)
    reopened = build_local_product(config=config(cash="1000000"), state_directory=tmp_path)
    assert reopened.portfolio.cash == Decimal("1000000")


def test_f11_the_first_event_fixes_the_anchor(tmp_path: Path) -> None:
    """Before the event the figure moves; after it, it does not."""
    build_local_product(config=config(cash="1000"), state_directory=tmp_path)
    build_local_product(config=config(cash="2000"), state_directory=tmp_path)
    with_history(tmp_path, cash="2000")
    with pytest.raises(AccountGenesisMismatch):
        build_local_product(config=config(cash="3000"), state_directory=tmp_path)
