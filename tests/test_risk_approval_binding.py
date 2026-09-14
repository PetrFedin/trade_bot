from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.store import OrderState
from app.risk.pretrade import PreTradeRiskEngine, RiskDecision, RiskLimits

NOW = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)


def engine() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("10000"),
            maximum_symbol_notional=Decimal("10000"),
            maximum_gross_notional=Decimal("10000"),
        )
    )


def intent(
    intent_id: str,
    *,
    symbol: str = "AAPL",
    quantity: str = "2",
    price: str = "100",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        created_at=NOW,
        strategy_id="f06-binding",
    )


def approve(value: OrderIntent) -> RiskDecision:
    return engine().evaluate(
        value,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
    )


def test_exact_risk_approval_binding_can_prepare_outbox(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "binding.sqlite")
    value = intent("exact")
    decision = approve(value)

    prepared = PaperOrderLifecycle(store).prepare(
        value,
        decision,
        occurred_at=NOW,
    )

    assert prepared.record.state is OrderState.OUTBOXED
    assert decision.intent_id == value.intent_id
    assert len(decision.intent_fingerprint) == 64
    assert len(store.pending_outbox()) == 1


def test_unrelated_approved_intent_is_rejected_before_any_oms_write(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "binding.sqlite")
    approved_for_a = approve(intent("intent-a"))
    different = intent("intent-b")

    with pytest.raises(ValueError, match="RISK_APPROVAL_INTENT_MISMATCH"):
        PaperOrderLifecycle(store).prepare(
            different,
            approved_for_a,
            occurred_at=NOW,
        )

    assert store.get(different.intent_id) is None
    assert store.pending_outbox() == ()


def test_changed_order_economics_are_rejected_even_with_same_intent_id(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "binding.sqlite")
    original = intent("same-id", quantity="1", price="100")
    approval = approve(original)
    repriced = replace(original, limit_price=Decimal("1000"))

    with pytest.raises(ValueError, match="RISK_APPROVAL_ECONOMICS_MISMATCH"):
        PaperOrderLifecycle(store).prepare(
            repriced,
            approval,
            occurred_at=NOW,
        )

    assert store.get(original.intent_id) is None


def test_same_notional_but_different_symbol_is_rejected_by_fingerprint(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "binding.sqlite")
    original = intent("same-id", symbol="AAPL", quantity="2", price="100")
    approval = approve(original)
    substituted = replace(original, symbol="MSFT")
    assert substituted.quantity * substituted.limit_price == approval.order_notional

    with pytest.raises(ValueError, match="RISK_APPROVAL_INTENT_MISMATCH"):
        PaperOrderLifecycle(store).prepare(
            substituted,
            approval,
            occurred_at=NOW,
        )

    assert store.get(original.intent_id) is None


def test_manual_approved_boolean_without_binding_is_rejected(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "binding.sqlite")
    value = intent("manual")
    unbound = RiskDecision(
        approved=True,
        reasons=(),
        order_notional=Decimal("200"),
        projected_symbol_notional=Decimal("200"),
        projected_gross_notional=Decimal("200"),
    )

    with pytest.raises(ValueError, match="RISK_APPROVAL_NOT_BOUND"):
        PaperOrderLifecycle(store).prepare(value, unbound, occurred_at=NOW)

    assert store.get(value.intent_id) is None
    assert store.pending_outbox() == ()
