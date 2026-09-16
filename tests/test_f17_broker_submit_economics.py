from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.paper_executor import PaperSubmitExecutor
from app.oms.store import DurableOmsStore, OrderState, OutboxMessage
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskDecision,
    RiskEvaluationMode,
    RiskLimits,
)
from app.runtime.paper_broker_contract_v99 import (
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
)

NOW = datetime(2026, 9, 16, 17, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="f17-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f17-strategy",
    )


def approved(value: OrderIntent) -> RiskDecision:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("10000"),
            maximum_symbol_notional=Decimal("10000"),
            maximum_gross_notional=Decimal("10000"),
        )
    ).evaluate(
        value,
        mode=RiskEvaluationMode.REPLAY,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
    )


class EvidencePaperBroker:
    paper_order_writes_enabled = True

    def __init__(self, *, transform=None, ambiguous: bool = False) -> None:
        self.transform = transform or (lambda order: order)
        self.ambiguous = ambiguous
        self.submit_calls = 0
        self.get_calls = 0
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        exact = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-f17",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )
        result = self.transform(exact)
        self.orders[kwargs["client_order_id"]] = result
        if self.ambiguous:
            raise BrokerMutationError("TIMEOUT", "submit outcome ambiguous", ambiguous=True)
        return result

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.orders.get(client_order_id)


def prepared(tmp_path):
    store = DurableOmsStore(tmp_path / "f17.sqlite")
    value = intent()
    PaperOrderLifecycle(store).prepare(value, approved(value), occurred_at=NOW)
    return store, store.pending_outbox()[0]


def uncertain_event(store: DurableOmsStore) -> dict[str, object]:
    matches = [
        event for event in store.events("f17-intent") if event["event_type"] == "UNCERTAIN"
    ]
    assert len(matches) == 1
    return matches[0]


def test_exact_broker_submit_economics_are_adopted(tmp_path) -> None:
    store, message = prepared(tmp_path)
    broker = EvidencePaperBroker()

    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)

    assert result.record.state is OrderState.ACKNOWLEDGED
    assert result.record.broker_order_id == "broker-f17"
    assert broker.submit_calls == 1
    assert store.pending_outbox() == ()


@pytest.mark.parametrize(
    ("transform", "mismatch"),
    (
        (lambda order: replace(order, client_order_id="other-client"), "client_order_id"),
        (lambda order: replace(order, instrument="MSFT"), "symbol"),
        (lambda order: replace(order, side=OrderSide.SELL), "side"),
        (lambda order: replace(order, quantity=Decimal("11")), "quantity"),
        (lambda order: replace(order, limit_price=Decimal("150")), "limit_price"),
    ),
)
def test_mismatched_broker_submit_truth_is_quarantined_with_evidence(
    tmp_path,
    transform,
    mismatch,
) -> None:
    store, message = prepared(tmp_path)
    broker = EvidencePaperBroker(transform=transform)

    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)

    assert result.record.state is OrderState.UNCERTAIN
    assert result.record.broker_order_id == ""
    assert result.record.filled_quantity == Decimal("0")
    assert result.mutation_attempted
    assert not result.recovered_by_read
    assert broker.submit_calls == 1
    assert store.pending_outbox() == ()

    event = uncertain_event(store)
    payload = event["payload"]
    assert payload["reason"] == "BROKER_SUBMIT_ECONOMICS_MISMATCH"
    assert mismatch in payload["mismatches"]
    assert payload["authorized"]["limit_price"] == "100"
    assert payload["broker_response"]["broker_order_id"] == "broker-f17"

    repeated = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert repeated.record.state is OrderState.UNCERTAIN
    assert broker.submit_calls == 1


def test_invalid_broker_identity_is_quarantined_without_adoption(tmp_path) -> None:
    store, message = prepared(tmp_path)
    broker = EvidencePaperBroker(
        transform=lambda order: replace(order, broker_order_id="")
    )

    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)

    assert result.record.state is OrderState.UNCERTAIN
    assert result.record.broker_order_id == ""
    event = uncertain_event(store)
    payload = event["payload"]
    assert payload["reason"] == "BROKER_SUBMIT_RESPONSE_INVALID"
    assert "broker_order_id is required" in payload["validation_error"]
    assert payload["broker_response"]["broker_order_id"] == ""


def test_ambiguous_submit_get_recovery_must_pass_same_economic_validation(tmp_path) -> None:
    store, message = prepared(tmp_path)
    broker = EvidencePaperBroker(
        transform=lambda order: replace(order, limit_price=Decimal("150")),
        ambiguous=True,
    )

    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)

    assert result.record.state is OrderState.UNCERTAIN
    assert result.record.broker_order_id == ""
    assert result.mutation_attempted
    assert result.recovered_by_read
    assert broker.submit_calls == 1
    assert broker.get_calls == 1
    event = uncertain_event(store)
    assert event["payload"]["reason"] == "BROKER_SUBMIT_ECONOMICS_MISMATCH"
    assert "limit_price" in event["payload"]["mismatches"]


def test_mismatched_filled_submit_response_cannot_move_oms_fill(tmp_path) -> None:
    store, message = prepared(tmp_path)
    broker = EvidencePaperBroker(
        transform=lambda order: replace(
            order,
            limit_price=Decimal("150"),
            status=BrokerOrderStatus.FILLED,
            filled_quantity=Decimal("10"),
            filled_avg_price=Decimal("150"),
        )
    )

    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)

    assert result.record.state is OrderState.UNCERTAIN
    assert result.record.filled_quantity == Decimal("0")
    assert result.record.broker_order_id == ""
    event = uncertain_event(store)
    assert event["payload"]["broker_response"]["status"] == "FILLED"
    assert event["payload"]["broker_response"]["filled_quantity"] == "10"


def test_tampered_submit_outbox_is_rejected_before_network_mutation(tmp_path) -> None:
    store, message = prepared(tmp_path)
    broker = EvidencePaperBroker()
    tampered = OutboxMessage(
        message_id=message.message_id,
        intent_id=message.intent_id,
        topic=message.topic,
        payload={**message.payload, "limit_price": "999"},
        created_at=message.created_at,
    )

    with pytest.raises(ValueError, match="SUBMIT_OUTBOX_ECONOMICS_MISMATCH"):
        PaperSubmitExecutor(store=store, broker=broker).execute(tampered, occurred_at=NOW)

    assert broker.submit_calls == 0
    persisted = store.get("f17-intent")
    assert persisted is not None and persisted.state is OrderState.OUTBOXED
    assert len(store.pending_outbox()) == 1
