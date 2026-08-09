from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import Fill, OrderIntent, Side
from app.oms.reconciliation import (
    BrokerOrderState,
    BrokerOrderTruth,
    BrokerPortfolioTruth,
    BrokerPositionTruth,
    OmsReconciler,
    reconcile_portfolio,
)
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import RiskDecision

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def intent(intent_id: str = "intent-1") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="validation-momentum",
    )


def approved() -> RiskDecision:
    return RiskDecision(
        approved=True,
        reasons=(),
        order_notional=Decimal("1000"),
        projected_symbol_notional=Decimal("1000"),
        projected_gross_notional=Decimal("1000"),
    )


def test_durable_order_lifecycle_outbox_and_monotonic_fills(tmp_path) -> None:
    db = tmp_path / "oms.sqlite"
    store = DurableOmsStore(db)
    lifecycle = PaperOrderLifecycle(store)

    prepared = lifecycle.prepare(intent(), approved(), occurred_at=NOW)
    assert prepared.record.state is OrderState.OUTBOXED
    assert prepared.client_order_id.startswith("astra-paper-")

    repeated = lifecycle.prepare(intent(), approved(), occurred_at=NOW)
    assert repeated.client_order_id == prepared.client_order_id
    assert len(store.pending_outbox()) == 1

    message = store.pending_outbox()[0]
    assert message.payload["symbol"] == "AAPL"
    store.mark_outbox_published(message.message_id, occurred_at=NOW)
    assert store.pending_outbox() == ()

    store.transition(
        "intent-1",
        OrderState.SUBMIT_STARTED,
        event_id="submit:1",
        occurred_at=NOW,
    )
    store.transition(
        "intent-1",
        OrderState.ACKNOWLEDGED,
        event_id="ack:1",
        occurred_at=NOW,
        broker_order_id="broker-1",
    )
    partial = store.apply_cumulative_fill(
        "intent-1",
        event_id="fill-event-1",
        cumulative_filled=Decimal("4"),
        occurred_at=NOW,
    )
    assert partial.state is OrderState.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("4")

    duplicate = store.apply_cumulative_fill(
        "intent-1",
        event_id="fill-event-1",
        cumulative_filled=Decimal("4"),
        occurred_at=NOW,
    )
    assert duplicate == partial

    with pytest.raises(ValueError, match="FILLED_QUANTITY_REGRESSION"):
        store.apply_cumulative_fill(
            "intent-1",
            event_id="fill-regression",
            cumulative_filled=Decimal("3"),
            occurred_at=NOW,
        )

    filled = store.apply_cumulative_fill(
        "intent-1",
        event_id="fill-event-2",
        cumulative_filled=Decimal("10"),
        occurred_at=NOW,
    )
    assert filled.state is OrderState.FILLED
    assert filled.terminal

    reopened = DurableOmsStore(db)
    persisted = reopened.get("intent-1")
    assert persisted is not None
    assert persisted.state is OrderState.FILLED
    assert persisted.broker_order_id == "broker-1"
    assert len(reopened.events("intent-1")) >= 7


def test_uncertain_order_uses_read_only_reconciliation(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "reconcile.sqlite")
    lifecycle = PaperOrderLifecycle(store)
    lifecycle.prepare(intent("intent-2"), approved(), occurred_at=NOW)
    store.transition(
        "intent-2",
        OrderState.SUBMIT_STARTED,
        event_id="submit:2",
        occurred_at=NOW,
    )
    store.transition(
        "intent-2",
        OrderState.UNCERTAIN,
        event_id="timeout:2",
        occurred_at=NOW,
        payload={"reason": "TRANSPORT_TIMEOUT"},
    )

    truth = BrokerOrderTruth(
        client_order_id=PaperOrderLifecycle(store).client_order_id(intent("intent-2")),
        broker_order_id="broker-2",
        state=BrokerOrderState.OPEN,
        cumulative_filled=Decimal("0"),
    )
    reconciled = OmsReconciler(store).reconcile_order(
        "intent-2",
        truth,
        occurred_at=NOW,
        event_prefix="reconcile-2",
    )
    assert reconciled.state is OrderState.ACKNOWLEDGED
    assert reconciled.broker_order_id == "broker-2"
    assert any(event["event_type"] == "RECONCILING" for event in store.events("intent-2"))


def test_missing_uncertain_order_escalates_to_manual_without_mutation(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "missing.sqlite")
    lifecycle = PaperOrderLifecycle(store)
    lifecycle.prepare(intent("intent-3"), approved(), occurred_at=NOW)
    store.transition("intent-3", OrderState.SUBMIT_STARTED, event_id="submit:3", occurred_at=NOW)
    store.transition("intent-3", OrderState.UNCERTAIN, event_id="timeout:3", occurred_at=NOW)
    result = OmsReconciler(store).reconcile_order(
        "intent-3",
        None,
        occurred_at=NOW,
        event_prefix="reconcile-3",
    )
    assert result.state is OrderState.MANUAL
    assert store.pending_outbox() != ()


def test_fee_aware_portfolio_pnl_and_broker_truth_reconciliation() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    ledger.apply_fill(
        Fill(
            fill_id="buy-1",
            order_intent_id="intent-buy",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            occurred_at=NOW,
            fee=Decimal("1"),
        )
    )
    ledger.apply_fill(
        Fill(
            fill_id="sell-1",
            order_intent_id="intent-sell",
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("4"),
            price=Decimal("110"),
            occurred_at=NOW,
            fee=Decimal("1"),
        )
    )
    snapshot = ledger.snapshot({"AAPL": Decimal("105")})
    assert snapshot.cash == Decimal("9438")
    assert snapshot.positions[0].quantity == Decimal("6")
    assert snapshot.realized_pnl == Decimal("38.6")
    assert snapshot.unrealized_pnl == Decimal("29.4")
    assert snapshot.total_pnl == Decimal("68")
    assert snapshot.fees_paid == Decimal("2")

    matched = reconcile_portfolio(
        ledger,
        BrokerPortfolioTruth(
            cash=Decimal("9438"),
            positions=(BrokerPositionTruth("AAPL", Decimal("6")),),
        ),
    )
    assert matched.matched
    assert matched.reasons == ()

    mismatch = reconcile_portfolio(
        ledger,
        BrokerPortfolioTruth(
            cash=Decimal("9437"),
            positions=(BrokerPositionTruth("AAPL", Decimal("5")),),
        ),
    )
    assert not mismatch.matched
    assert "CASH_MISMATCH" in mismatch.reasons
    assert "POSITION_MISMATCH:AAPL" in mismatch.reasons
