from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

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


class DispatchAuthorizer(Protocol):
    def __call__(
        self,
        record: OrderRecord,
        *,
        occurred_at: datetime,
    ) -> object: ...


@dataclass(frozen=True)
class ExecutionResult:
    record: OrderRecord
    mutation_attempted: bool
    recovered_by_read: bool


class PaperSubmitExecutor:
    """At-most-one paper submit executor with fail-closed broker truth adoption.

    Every execution attempt uses a unique claim event for the transactional
    ``OUTBOXED -> SUBMIT_STARTED`` transition. Exactly one concurrent caller can
    own that transition; stale readers lose the claim and therefore never POST.

    The durable submit outbox is validated against the authoritative OMS order
    before any broker mutation. After a POST or GET recovery, broker-returned
    identity/economics are validated before broker id, state or fills are adopted.
    A response that cannot be proven to describe the authorized order becomes
    durable ``UNCERTAIN`` evidence instead of a successful acknowledgement.

    When a final-dispatch authorizer is configured it is evaluated immediately
    before the submit claim. A blocked dispatch therefore leaves the order
    OUTBOXED and the outbox message unpublished. A fresh ``SUBMIT_STARTED`` marker
    is protected by a bounded recovery grace; stale restart recovery is GET-only.
    """

    def __init__(
        self,
        *,
        store: OmsExecutionStore,
        broker: PaperBrokerV99,
        started_recovery_grace_seconds: float = 30.0,
        dispatch_authorizer: DispatchAuthorizer | None = None,
    ) -> None:
        grace = float(started_recovery_grace_seconds)
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("started_recovery_grace_seconds must be finite and non-negative")
        self.store = store
        self.broker = broker
        self.started_recovery_grace_seconds = grace
        self.dispatch_authorizer = dispatch_authorizer

    @staticmethod
    def _time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _authorized_submit_payload(record: OrderRecord) -> dict[str, object]:
        return {
            "intent_id": record.intent_id,
            "client_order_id": record.client_order_id,
            "symbol": record.symbol,
            "side": record.side.value,
            "quantity": str(record.quantity),
            "limit_price": str(record.limit_price),
        }

    @classmethod
    def _validate_submit_message(
        cls,
        record: OrderRecord,
        message: OutboxMessage,
    ) -> None:
        if message.intent_id != record.intent_id:
            raise ValueError("SUBMIT_OUTBOX_INTENT_MISMATCH")
        if message.topic != "paper_order_submit":
            raise ValueError("SUBMIT_OUTBOX_TOPIC_MISMATCH")
        if message.payload != cls._authorized_submit_payload(record):
            raise ValueError("SUBMIT_OUTBOX_ECONOMICS_MISMATCH")

    @staticmethod
    def _broker_order_evidence(order: BrokerOrder) -> dict[str, object]:
        return {
            "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id,
            "instrument": order.instrument,
            "side": order.side.value,
            "quantity": str(order.quantity),
            "limit_price": str(order.limit_price),
            "status": order.status.value,
            "filled_quantity": str(order.filled_quantity),
            "filled_avg_price": (
                None if order.filled_avg_price is None else str(order.filled_avg_price)
            ),
            "updated_at": order.updated_at.isoformat(),
        }

    @staticmethod
    def _broker_truth_mismatches(
        local: OrderRecord,
        order: BrokerOrder,
    ) -> tuple[tuple[str, ...], str | None]:
        validation_error: str | None = None
        try:
            order.validate()
        except (TypeError, ValueError) as exc:
            validation_error = f"{type(exc).__name__}:{exc}"

        mismatches: list[str] = []
        if order.client_order_id != local.client_order_id:
            mismatches.append("client_order_id")
        if order.instrument != local.symbol:
            mismatches.append("symbol")
        if order.side.value != local.side.value:
            mismatches.append("side")
        if order.quantity != local.quantity:
            mismatches.append("quantity")
        # The current broker contract has no venue-specific normalization policy.
        # Economic equality is therefore exact; a future tolerance must be explicit
        # and separately qualified rather than inferred here.
        if order.limit_price != local.limit_price:
            mismatches.append("limit_price")
        return tuple(mismatches), validation_error

    def execute(self, message: OutboxMessage, *, occurred_at: datetime) -> ExecutionResult:
        moment = self._time(occurred_at)
        record = self.store.get(message.intent_id)
        if record is None:
            raise KeyError(message.intent_id)
        if not bool(getattr(self.broker, "paper_order_writes_enabled", False)):
            raise ValueError("PAPER_ORDER_WRITES_DISABLED")

        if record.state is OrderState.OUTBOXED:
            self._validate_submit_message(record, message)
            if self.dispatch_authorizer is not None:
                self.dispatch_authorizer(record, occurred_at=moment)
            try:
                record = self.store.transition(
                    record.intent_id,
                    OrderState.SUBMIT_STARTED,
                    event_id=f"submit-claim:{message.message_id}:{uuid4().hex}",
                    occurred_at=moment,
                )
            except ValueError:
                latest = self.store.get(message.intent_id)
                if latest is None:
                    raise KeyError(message.intent_id) from None
                if latest.state is OrderState.OUTBOXED:
                    raise
                return self._handle_existing(latest, message, occurred_at=moment)
            return self._attempt_submit(record, message, occurred_at=moment)

        return self._handle_existing(record, message, occurred_at=moment)

    def _handle_existing(
        self,
        record: OrderRecord,
        message: OutboxMessage,
        *,
        occurred_at: datetime,
    ) -> ExecutionResult:
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
        started_at = self._time(record.updated_at)
        age_seconds = (occurred_at - started_at).total_seconds()
        if age_seconds < self.started_recovery_grace_seconds:
            return ExecutionResult(record=record, mutation_attempted=False, recovered_by_read=False)

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
        local = self.store.get(intent_id)
        if local is None:
            raise KeyError(intent_id)
        mismatches, validation_error = self._broker_truth_mismatches(local, order)
        if validation_error is not None or mismatches:
            return self._quarantine_broker_truth(
                local,
                order,
                mismatches=mismatches,
                validation_error=validation_error,
                event_prefix=event_prefix,
                occurred_at=occurred_at,
            )

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

    def _quarantine_broker_truth(
        self,
        local: OrderRecord,
        order: BrokerOrder,
        *,
        mismatches: tuple[str, ...],
        validation_error: str | None,
        event_prefix: str,
        occurred_at: datetime,
    ) -> OrderRecord:
        payload: dict[str, object] = {
            "reason": (
                "BROKER_SUBMIT_RESPONSE_INVALID"
                if validation_error is not None
                else "BROKER_SUBMIT_ECONOMICS_MISMATCH"
            ),
            "mismatches": list(mismatches),
            "authorized": self._authorized_submit_payload(local),
            "broker_response": self._broker_order_evidence(order),
        }
        if validation_error is not None:
            payload["validation_error"] = validation_error
        return self.store.transition(
            local.intent_id,
            OrderState.UNCERTAIN,
            event_id=f"{event_prefix}:broker-truth-uncertain",
            occurred_at=occurred_at,
            payload=payload,
        )
