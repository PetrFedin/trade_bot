from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.paper_executor import PaperSubmitExecutor
from app.oms.reconciliation import BrokerOrderState, BrokerOrderTruth, OmsReconciler
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


def intent(intent_id: str = "f17-intent") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
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


def uncertain_store(tmp_path, *, reason: str) -> DurableOmsStore:
    store, _ = prepared(tmp_path)
    store.transition(
        "f17-intent",
        OrderState.SUBMIT_STARTED,
        event_id="f17-reconcile-submit-started",
        occurred_at=NOW,
    )
    store.transition(
        "f17-intent",
        OrderState.UNCERTAIN,
        event_id="f17-reconcile-uncertain",
        occurred_at=NOW,
        payload={"reason": reason},
    )
    return store


def broker_truth(
    store: DurableOmsStore,
    *,
    state: BrokerOrderState = BrokerOrderState.OPEN,
    cumulative_filled: Decimal = Decimal("0"),
    include_economics: bool = True,
) -> BrokerOrderTruth:
    local = store.get("f17-intent")
    assert local is not None
    kwargs: dict[str, object] = {}
    if include_economics:
        kwargs = {
            "symbol": local.symbol,
            "side": local.side,
            "quantity": local.quantity,
            "limit_price": local.limit_price,
        }
    return BrokerOrderTruth(
        client_order_id=local.client_order_id,
        broker_order_id="broker-f17-reconciled",
        state=state,
        cumulative_filled=cumulative_filled,
        **kwargs,
    )


def acknowledged_store(tmp_path) -> DurableOmsStore:
    store, _ = prepared(tmp_path)
    store.transition(
        "f17-intent",
        OrderState.SUBMIT_STARTED,
        event_id="f17-ack-submit-started",
        occurred_at=NOW,
    )
    store.transition(
        "f17-intent",
        OrderState.ACKNOWLEDGED,
        event_id="f17-ack",
        occurred_at=NOW,
        broker_order_id="broker-f17-reconciled",
    )
    return store


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


@pytest.mark.parametrize(
    ("changes", "error"),
    (
        ({"client_order_id": ""}, "broker order identity is required"),
        ({"broker_order_id": ""}, "broker order identity is required"),
        ({"cumulative_filled": Decimal("-1")}, "cumulative_filled"),
        ({"symbol": "AAPL"}, "BROKER_ORDER_ECONOMICS_INCOMPLETE"),
        (
            {
                "symbol": "aapl",
                "side": Side.BUY,
                "quantity": Decimal("10"),
                "limit_price": Decimal("100"),
            },
            "broker symbol",
        ),
        (
            {
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": Decimal("10"),
                "limit_price": Decimal("100"),
            },
            "broker side is invalid",
        ),
        (
            {
                "symbol": "AAPL",
                "side": Side.BUY,
                "quantity": Decimal("0"),
                "limit_price": Decimal("100"),
            },
            "broker quantity",
        ),
        (
            {
                "symbol": "AAPL",
                "side": Side.BUY,
                "quantity": Decimal("10"),
                "limit_price": Decimal("0"),
            },
            "broker limit_price",
        ),
    ),
)
def test_broker_order_truth_validation_fails_closed(changes, error) -> None:
    truth = BrokerOrderTruth(
        client_order_id="client",
        broker_order_id="broker",
        state=BrokerOrderState.OPEN,
        cumulative_filled=Decimal("0"),
    )
    with pytest.raises(ValueError, match=error):
        replace(truth, **changes).validate()


def test_broker_order_truth_allows_identity_only_or_complete_economics() -> None:
    identity_only = BrokerOrderTruth(
        client_order_id="client",
        broker_order_id="broker",
        state=BrokerOrderState.OPEN,
        cumulative_filled=Decimal("0"),
    )
    identity_only.validate()
    assert not identity_only.has_complete_economics

    complete = replace(
        identity_only,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
    )
    complete.validate()
    assert complete.has_complete_economics


def test_f17_quarantine_requires_complete_economics_before_reconciliation(tmp_path) -> None:
    store = uncertain_store(tmp_path, reason="BROKER_SUBMIT_ECONOMICS_MISMATCH")
    truth = broker_truth(store, include_economics=False)

    with pytest.raises(ValueError, match="BROKER_ECONOMICS_REQUIRED_FOR_RECONCILIATION"):
        OmsReconciler(store).reconcile_order(
            "f17-intent",
            truth,
            occurred_at=NOW,
            event_prefix="f17-incomplete",
        )

    persisted = store.get("f17-intent")
    assert persisted is not None and persisted.state is OrderState.UNCERTAIN


@pytest.mark.parametrize(
    ("changes", "field"),
    (
        ({"symbol": "MSFT"}, "symbol"),
        ({"side": Side.SELL}, "side"),
        ({"quantity": Decimal("9")}, "quantity"),
        ({"limit_price": Decimal("101")}, "limit_price"),
    ),
)
def test_f17_quarantine_rejects_changed_reconciliation_economics(
    tmp_path,
    changes,
    field,
) -> None:
    store = uncertain_store(tmp_path, reason="BROKER_SUBMIT_ECONOMICS_MISMATCH")
    truth = replace(broker_truth(store), **changes)

    with pytest.raises(ValueError, match=rf"BROKER_ECONOMICS_MISMATCH:.*{field}"):
        OmsReconciler(store).reconcile_order(
            "f17-intent",
            truth,
            occurred_at=NOW,
            event_prefix=f"f17-mismatch-{field}",
        )

    persisted = store.get("f17-intent")
    assert persisted is not None and persisted.state is OrderState.UNCERTAIN


def test_f17_quarantine_exact_economics_can_restore_acknowledged_truth(tmp_path) -> None:
    store = uncertain_store(tmp_path, reason="BROKER_SUBMIT_ECONOMICS_MISMATCH")

    result = OmsReconciler(store).reconcile_order(
        "f17-intent",
        broker_truth(store),
        occurred_at=NOW,
        event_prefix="f17-exact",
    )

    assert result.state is OrderState.ACKNOWLEDGED
    assert result.broker_order_id == "broker-f17-reconciled"
    event_types = tuple(event["event_type"] for event in store.events("f17-intent"))
    assert "RECONCILING" in event_types
    assert "RECONCILED" in event_types


def test_non_f17_uncertainty_keeps_identity_only_reconciliation_path(tmp_path) -> None:
    store = uncertain_store(tmp_path, reason="TRANSPORT_TIMEOUT")

    result = OmsReconciler(store).reconcile_order(
        "f17-intent",
        broker_truth(store, include_economics=False),
        occurred_at=NOW,
        event_prefix="legacy-uncertain",
    )

    assert result.state is OrderState.ACKNOWLEDGED
    assert result.broker_order_id == "broker-f17-reconciled"


def test_missing_broker_truth_from_uncertain_escalates_to_manual(tmp_path) -> None:
    store = uncertain_store(tmp_path, reason="TRANSPORT_TIMEOUT")

    result = OmsReconciler(store).reconcile_order(
        "f17-intent",
        None,
        occurred_at=NOW,
        event_prefix="missing-uncertain",
    )

    assert result.state is OrderState.MANUAL
    assert store.events("f17-intent")[-1]["payload"]["reason"] == "BROKER_ORDER_NOT_FOUND"


def test_missing_broker_truth_for_non_uncertain_order_is_rejected(tmp_path) -> None:
    store, _ = prepared(tmp_path)

    with pytest.raises(ValueError, match="BROKER_ORDER_NOT_FOUND"):
        OmsReconciler(store).reconcile_order(
            "f17-intent",
            None,
            occurred_at=NOW,
            event_prefix="missing-outboxed",
        )


def test_reconciliation_rejects_client_identity_and_fill_overflow(tmp_path) -> None:
    store = acknowledged_store(tmp_path)
    exact = broker_truth(store)

    with pytest.raises(ValueError, match="CLIENT_ORDER_ID_MISMATCH"):
        OmsReconciler(store).reconcile_order(
            "f17-intent",
            replace(exact, client_order_id="different-client"),
            occurred_at=NOW,
            event_prefix="client-mismatch",
        )

    with pytest.raises(ValueError, match="BROKER_FILL_EXCEEDS_LOCAL_ORDER"):
        OmsReconciler(store).reconcile_order(
            "f17-intent",
            replace(exact, cumulative_filled=Decimal("11")),
            occurred_at=NOW,
            event_prefix="fill-overflow",
        )


def test_reconciliation_rejects_inconsistent_filled_and_partial_states(tmp_path) -> None:
    filled_store = acknowledged_store(tmp_path / "filled")
    with pytest.raises(ValueError, match="FILLED_STATE_WITH_INCOMPLETE_QUANTITY"):
        OmsReconciler(filled_store).reconcile_order(
            "f17-intent",
            broker_truth(
                filled_store,
                state=BrokerOrderState.FILLED,
                cumulative_filled=Decimal("5"),
            ),
            occurred_at=NOW,
            event_prefix="filled-incomplete",
        )

    partial_zero_store = acknowledged_store(tmp_path / "partial-zero")
    with pytest.raises(ValueError, match="PARTIAL_STATE_WITH_INVALID_QUANTITY"):
        OmsReconciler(partial_zero_store).reconcile_order(
            "f17-intent",
            broker_truth(
                partial_zero_store,
                state=BrokerOrderState.PARTIALLY_FILLED,
                cumulative_filled=Decimal("0"),
            ),
            occurred_at=NOW,
            event_prefix="partial-zero",
        )

    partial_full_store = acknowledged_store(tmp_path / "partial-full")
    with pytest.raises(ValueError, match="PARTIAL_STATE_WITH_INVALID_QUANTITY"):
        OmsReconciler(partial_full_store).reconcile_order(
            "f17-intent",
            broker_truth(
                partial_full_store,
                state=BrokerOrderState.PARTIALLY_FILLED,
                cumulative_filled=Decimal("10"),
            ),
            occurred_at=NOW,
            event_prefix="partial-full",
        )


def test_reconciliation_applies_partial_then_full_fill_monotonically(tmp_path) -> None:
    store = acknowledged_store(tmp_path)
    reconciler = OmsReconciler(store)

    partial = reconciler.reconcile_order(
        "f17-intent",
        broker_truth(
            store,
            state=BrokerOrderState.PARTIALLY_FILLED,
            cumulative_filled=Decimal("4"),
        ),
        occurred_at=NOW,
        event_prefix="partial-four",
    )
    assert partial.state is OrderState.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("4")

    filled = reconciler.reconcile_order(
        "f17-intent",
        broker_truth(
            store,
            state=BrokerOrderState.FILLED,
            cumulative_filled=Decimal("10"),
        ),
        occurred_at=NOW,
        event_prefix="filled-ten",
    )
    assert filled.state is OrderState.FILLED
    assert filled.filled_quantity == Decimal("10")

    terminal = reconciler.reconcile_order(
        "f17-intent",
        None,
        occurred_at=NOW,
        event_prefix="terminal-missing",
    )
    assert terminal.state is OrderState.FILLED


def test_reconciliation_open_truth_is_idempotent_when_already_acknowledged(tmp_path) -> None:
    store = acknowledged_store(tmp_path)
    before = store.get("f17-intent")
    assert before is not None

    after = OmsReconciler(store).reconcile_order(
        "f17-intent",
        broker_truth(store, state=BrokerOrderState.OPEN),
        occurred_at=NOW,
        event_prefix="open-repeat",
    )

    assert after == before


def test_reconciliation_maps_cancelled_and_rejected_truth(tmp_path) -> None:
    cancelled_store = acknowledged_store(tmp_path / "cancelled")
    cancelled = OmsReconciler(cancelled_store).reconcile_order(
        "f17-intent",
        broker_truth(cancelled_store, state=BrokerOrderState.CANCELLED),
        occurred_at=NOW,
        event_prefix="cancelled",
    )
    assert cancelled.state is OrderState.CANCELLED

    rejected_store, _ = prepared(tmp_path / "rejected")
    rejected_store.transition(
        "f17-intent",
        OrderState.SUBMIT_STARTED,
        event_id="rejected-submit-started",
        occurred_at=NOW,
    )
    rejected = OmsReconciler(rejected_store).reconcile_order(
        "f17-intent",
        broker_truth(rejected_store, state=BrokerOrderState.REJECTED),
        occurred_at=NOW,
        event_prefix="rejected",
    )
    assert rejected.state is OrderState.REJECTED


def test_reconciliation_unknown_local_intent_is_key_error(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "unknown.sqlite")
    truth = BrokerOrderTruth(
        client_order_id="client",
        broker_order_id="broker",
        state=BrokerOrderState.OPEN,
        cumulative_filled=Decimal("0"),
    )

    with pytest.raises(KeyError, match="missing-intent"):
        OmsReconciler(store).reconcile_order(
            "missing-intent",
            truth,
            occurred_at=NOW,
            event_prefix="missing-local",
        )
