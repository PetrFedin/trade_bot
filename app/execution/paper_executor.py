from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.oms.store import OrderRecord, OrderState, OutboxMessage
from app.runtime.paper_broker_contract_v99 import (
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
    PaperBrokerV99,
)


class OmsExecutionStore(Protocol):
    def get(self, intent_id: str) -> OrderRecord | None: ...

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        event_id: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> OrderRecord: ...

    def apply_cumulative_fill(
        self,
        intent_id: str,
        *,
        event_id: str,
        cumulative_filled: Decimal,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderRecord: ...

    def mark_outbox_published(self, message_id: int, *, occurred_at: datetime) -> None: ...


@dataclass(frozen=True)
class ExecutionResult:
    record: OrderRecord
    mutation_attempted: bool
    recovered_by_read: bool


class PaperSubmitExecutor:
    """At-most-one paper submit executor with GET-only ambiguity recovery.

    `SUBMIT_STARTED` is persisted before the network mutation. If the process later
    sees an outbox message in that state, it must reconcile by GET and must never POST
    again. This intentionally prefers a missed order over a duplicate mutation.
    """

    def __init__(self, *, store: OmsExecutionStore, broker: PaperBrokerV99) -> None:
        self.store = store
        self.broker = broker

    def execute(self, message: OutboxMessage, *, occurred_at: datetime) -> ExecutionResult:
        record = self.store.get(message.intent_id)
        if record is None:
            raise KeyError(message.intent_id)
        if not bool(getattr(self.broker, "paper_order_writes_enabled", False)):
            raise ValueError("PAPER_ORDER_WRITES_DISABLED")

        if record.state is OrderState.OUTBOXED:
            record = self.store.transition(
                record.intent_id,
                OrderState.SUBMIT_STARTED,
                event_id=f"submit-start:{message.message_id}",
                occurred_at=occurred_at,
            )
            return self._attempt_submit(record, message, occurred_at=occurred_at)

        if record.state is OrderState.SUBMIT_STARTED:
            return self._recover_after_started(record, message, occurred_at=occurred_at)

        if record.state in {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.UNCERTAIN,
            OrderState.RECONCILING,
            OrderState.RECONCILED,
            OrderState.MANUAL,
        }:
            self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
            return ExecutionResult(record=record, mutation_attempted=False, recovered_by_read=False)

        raise ValueError(f"OUTBOX_NOT_EXECUTABLE:{record.state.value}")

    def _attempt_submit(
        self,
        record: OrderRecord,
        message: OutboxMessage,
        *,
        occurred_at: datetime,
    ) -> ExecutionResult:
        try:
            broker_order = self.broker.submit_limit_order(
                client_order_id=record.client_order_id,
                instrument=record.symbol,
                side=OrderSide(record.side.value),
                quantity=record.quantity,
                limit_price=record.limit_price,
            )
        except BrokerMutationError as exc:
            if not exc.ambiguous:
                rejected = self.store.transition(
                    record.intent_id,
                    OrderState.REJECTED,
                    event_id=f"submit-rejected:{message.message_id}",
                    occurred_at=occurred_at,
                    payload={"code": exc.code},
                )
                self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
                return ExecutionResult(rejected, mutation_attempted=True, recovered_by_read=False)
            broker_order = self.broker.get_order_by_client_order_id(record.client_order_id)
            if broker_order is None:
                uncertain = self.store.transition(
                    record.intent_id,
                    OrderState.UNCERTAIN,
                    event_id=f"submit-uncertain:{message.message_id}",
                    occurred_at=occurred_at,
                    payload={"code": exc.code},
                )
                self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
                return ExecutionResult(uncertain, mutation_attempted=True, recovered_by_read=True)
            resolved = self._adopt_broker_truth(
                record.intent_id,
                broker_order,
                event_prefix=f"submit-recovered:{message.message_id}",
                occurred_at=occurred_at,
            )
            self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
            return ExecutionResult(resolved, mutation_attempted=True, recovered_by_read=True)

        resolved = self._adopt_broker_truth(
            record.intent_id,
            broker_order,
            event_prefix=f"submit-ack:{message.message_id}",
            occurred_at=occurred_at,
        )
        self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
        return ExecutionResult(resolved, mutation_attempted=True, recovered_by_read=False)

    def _recover_after_started(
        self,
        record: OrderRecord,
        message: OutboxMessage,
        *,
        occurred_at: datetime,
    ) -> ExecutionResult:
        broker_order = self.broker.get_order_by_client_order_id(record.client_order_id)
        if broker_order is None:
            uncertain = self.store.transition(
                record.intent_id,
                OrderState.UNCERTAIN,
                event_id=f"started-recovery-missing:{message.message_id}",
                occurred_at=occurred_at,
                payload={"reason": "NO_ORDER_AFTER_SUBMIT_STARTED"},
            )
            self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
            return ExecutionResult(uncertain, mutation_attempted=False, recovered_by_read=True)
        resolved = self._adopt_broker_truth(
            record.intent_id,
            broker_order,
            event_prefix=f"started-recovery:{message.message_id}",
            occurred_at=occurred_at,
        )
        self.store.mark_outbox_published(message.message_id, occurred_at=occurred_at)
        return ExecutionResult(resolved, mutation_attempted=False, recovered_by_read=True)

    def _adopt_broker_truth(
        self,
        intent_id: str,
        order: BrokerOrder,
        *,
        event_prefix: str,
        occurred_at: datetime,
    ) -> OrderRecord:
        order.validate()
        local = self.store.get(intent_id)
        if local is None:
            raise KeyError(intent_id)
        if order.client_order_id != local.client_order_id:
            raise ValueError("BROKER_CLIENT_ORDER_ID_MISMATCH")
        if order.instrument != local.symbol:
            raise ValueError("BROKER_SYMBOL_MISMATCH")
        if order.side.value != local.side.value:
            raise ValueError("BROKER_SIDE_MISMATCH")
        if order.quantity != local.quantity:
            raise ValueError("BROKER_QUANTITY_MISMATCH")

        if order.status is BrokerOrderStatus.REJECTED:
            return self.store.transition(
                intent_id,
                OrderState.REJECTED,
                event_id=f"{event_prefix}:rejected",
                occurred_at=occurred_at,
                broker_order_id=order.broker_order_id,
            )

        if local.state is OrderState.SUBMIT_STARTED:
            local = self.store.transition(
                intent_id,
                OrderState.ACKNOWLEDGED,
                event_id=f"{event_prefix}:ack",
                occurred_at=occurred_at,
                broker_order_id=order.broker_order_id,
            )

        if order.filled_quantity > local.filled_quantity:
            local = self.store.apply_cumulative_fill(
                intent_id,
                event_id=f"{event_prefix}:fill:{order.filled_quantity}",
                cumulative_filled=order.filled_quantity,
                occurred_at=occurred_at,
                broker_order_id=order.broker_order_id,
            )

        if order.status is BrokerOrderStatus.CANCELLED and local.state is not OrderState.CANCELLED:
            local = self.store.transition(
                intent_id,
                OrderState.CANCELLED,
                event_id=f"{event_prefix}:cancelled",
                occurred_at=occurred_at,
                broker_order_id=order.broker_order_id,
            )
        return local
