from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from app.domain.trading import Side
from app.execution.execution_checkpoints import (
    ExecutionCheckpoint,
    ExecutionCheckpointStore,
    canonical_execution_checkpoint_id,
)
from app.execution.execution_facts import ExecutionFactStore
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
    """At-most-one paper submit executor with fail-closed execution convergence.

    Every execution attempt uses a unique claim event for the transactional
    ``OUTBOXED -> SUBMIT_STARTED`` transition. Exactly one concurrent caller can
    own that transition; stale readers lose the claim and therefore never POST.

    Durable submit economics are validated before mutation. Broker-returned order
    identity/economics are validated before local adoption. A submit response that
    already reports cumulative execution is recorded as a durable checkpoint, never
    converted into a fabricated exact fill. Exact stream/activity executions later
    enter ``PaperTradeFillAccounting`` and resolve that barrier only after durable
    portfolio projection.

    BUY dispatch is blocked while either exact execution facts or aggregate execution
    checkpoints are unresolved. This closes the race where a second outbox item was
    prepared before the first order's execution/accounting divergence became visible.
    """

    def __init__(
        self,
        *,
        store: OmsExecutionStore,
        broker: PaperBrokerV99,
        started_recovery_grace_seconds: float = 30.0,
        dispatch_authorizer: DispatchAuthorizer | None = None,
        execution_facts: ExecutionFactStore | None = None,
        execution_checkpoints: ExecutionCheckpointStore | None = None,
    ) -> None:
        grace = float(started_recovery_grace_seconds)
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("started_recovery_grace_seconds must be finite and non-negative")
        self.store = store
        self.broker = broker
        self.started_recovery_grace_seconds = grace
        self.dispatch_authorizer = dispatch_authorizer
        self.execution_facts = execution_facts
        self.execution_checkpoints = execution_checkpoints

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
        if order.limit_price != local.limit_price:
            mismatches.append("limit_price")
        return tuple(mismatches), validation_error

    def _execution_convergence_gate(self, record: OrderRecord) -> None:
        if record.side is not Side.BUY:
            return
        if self.execution_facts is not None and self.execution_facts.unresolved_count() > 0:
            raise RuntimeError("EXECUTION_ACCOUNTING_NOT_CONVERGED")
        if (
            self.execution_checkpoints is not None
            and self.execution_checkpoints.unresolved_count() > 0
        ):
            raise RuntimeError("EXECUTION_ACCOUNTING_NOT_CONVERGED")

    def execute(self, message: OutboxMessage, *, occurred_at: datetime) -> ExecutionResult:
        moment = self._time(occurred_at)
        record = self.store.get(message.intent_id)
        if record is None:
            raise KeyError(message.intent_id)
        if not bool(getattr(self.broker, "paper_order_writes_enabled", False)):
            raise ValueError("PAPER_ORDER_WRITES_DISABLED")

        if record.state is OrderState.OUTBOXED:
            self._validate_submit_message(record, message)
            self._execution_convergence_gate(record)
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

        checkpoint_failure = self._checkpoint_broker_execution(
            local,
            order,
            event_prefix=event_prefix,
            occurred_at=occurred_at,
        )
        if checkpoint_failure is not None:
            return checkpoint_failure

        if order.status is BrokerOrderStatus.REJECTED:
            if order.filled_quantity > 0:
                return self.store.transition(
                    intent_id,
                    OrderState.UNCERTAIN,
                    event_id=f"{event_prefix}:execution-status-conflict",
                    occurred_at=occurred_at,
                    payload={
                        "reason": "BROKER_EXECUTION_STATUS_CONFLICT",
                        "broker_response": self._broker_order_evidence(order),
                    },
                )
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

        # Aggregate order truth does not contain exact execution identity/fee. Once a
        # cumulative execution has been checkpointed, leave exact OMS fill progression
        # to PaperTradeFillAccounting so OMS and portfolio advance from one fact source.
        if order.filled_quantity > 0:
            return local

        if order.status is BrokerOrderStatus.CANCELLED and local.state is not OrderState.CANCELLED:
            local = self.store.transition(
                intent_id,
                OrderState.CANCELLED,
                event_id=f"{event_prefix}:cancelled",
                occurred_at=occurred_at,
                broker_order_id=order.broker_order_id,
            )
        return local

    def _checkpoint_broker_execution(
        self,
        local: OrderRecord,
        order: BrokerOrder,
        *,
        event_prefix: str,
        occurred_at: datetime,
    ) -> OrderRecord | None:
        if order.filled_quantity <= 0:
            return None
        if self.execution_checkpoints is None:
            return self.store.transition(
                local.intent_id,
                OrderState.UNCERTAIN,
                event_id=f"{event_prefix}:execution-checkpoint-required",
                occurred_at=occurred_at,
                payload={
                    "reason": "EXECUTION_CHECKPOINT_STORE_REQUIRED",
                    "broker_response": self._broker_order_evidence(order),
                },
            )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id=canonical_execution_checkpoint_id(
                intent_id=local.intent_id,
                broker_order_id=order.broker_order_id,
                cumulative_quantity=order.filled_quantity,
                observed_at=order.updated_at,
            ),
            intent_id=local.intent_id,
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            symbol=order.instrument,
            side=local.side,
            order_quantity=order.quantity,
            cumulative_quantity=order.filled_quantity,
            broker_status=order.status.value,
            observed_avg_price=order.filled_avg_price,
            observed_at=order.updated_at,
        )
        try:
            self.execution_checkpoints.append(checkpoint)
        except ValueError as exc:
            if str(exc) != "EXECUTION_CHECKPOINT_CONFLICT":
                raise
            return self.store.transition(
                local.intent_id,
                OrderState.UNCERTAIN,
                event_id=f"{event_prefix}:execution-checkpoint-conflict",
                occurred_at=occurred_at,
                payload={
                    "reason": "EXECUTION_CHECKPOINT_CONFLICT",
                    "broker_response": self._broker_order_evidence(order),
                },
            )
        self.execution_checkpoints.resolve_from_projected_facts(
            intent_id=local.intent_id,
            occurred_at=occurred_at,
        )
        return None

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
