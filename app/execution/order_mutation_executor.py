from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.oms.order_mutations import (
    MutationKind,
    MutationOutboxMessage,
    MutationState,
    MutationStore,
    OrderMutationRecord,
)
from app.oms.store import OrderRecord, OrderState
from app.runtime.paper_broker_contract_v99 import (
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    PaperBrokerV99,
)


class MutationOmsStore(Protocol):
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


@dataclass(frozen=True)
class MutationExecutionResult:
    record: OrderRecord
    mutation: OrderMutationRecord
    mutation_attempted: bool
    recovered_by_read: bool


class PaperOrderMutationExecutor:
    """At-most-one cancel/replace executor with read-only recovery after STARTED.

    The mutation journal records STARTED before any broker DELETE/PATCH. Once STARTED
    is durable, retries and crash recovery are GET-only. This intentionally prefers a
    missed mutation over a duplicate mutation when the outcome cannot be proven.
    """

    def __init__(
        self,
        *,
        oms: MutationOmsStore,
        mutations: MutationStore,
        broker: PaperBrokerV99,
    ) -> None:
        self.oms = oms
        self.mutations = mutations
        self.broker = broker

    def execute(
        self,
        message: MutationOutboxMessage,
        *,
        occurred_at: datetime,
    ) -> MutationExecutionResult:
        mutation = self.mutations.get(message.mutation_id)
        if mutation is None:
            raise KeyError(message.mutation_id)
        if mutation.intent_id != message.intent_id:
            raise ValueError("MUTATION_OUTBOX_INTENT_MISMATCH")
        if not bool(getattr(self.broker, "paper_order_writes_enabled", False)):
            raise ValueError("PAPER_ORDER_WRITES_DISABLED")

        if mutation.state is MutationState.REQUESTED:
            mutation = self.mutations.mark_started(
                mutation.mutation_id, occurred_at=occurred_at
            )
            return self._attempt(mutation, message, occurred_at=occurred_at)

        if mutation.state is MutationState.STARTED:
            result = self._recover(mutation, occurred_at=occurred_at)
            self.mutations.mark_outbox_published(message.message_id, occurred_at=occurred_at)
            return result

        if mutation.state in {
            MutationState.SUCCEEDED,
            MutationState.FAILED,
            MutationState.UNCERTAIN,
        }:
            self.mutations.mark_outbox_published(message.message_id, occurred_at=occurred_at)
            order = self._order(mutation.intent_id)
            return MutationExecutionResult(
                record=order,
                mutation=mutation,
                mutation_attempted=False,
                recovered_by_read=False,
            )

        raise ValueError(f"MUTATION_NOT_EXECUTABLE:{mutation.state.value}")

    def reconcile(
        self,
        mutation_id: str,
        *,
        occurred_at: datetime,
    ) -> MutationExecutionResult:
        """Resolve STARTED/UNCERTAIN mutations using broker reads only."""

        mutation = self.mutations.get(mutation_id)
        if mutation is None:
            raise KeyError(mutation_id)
        if mutation.state not in {MutationState.STARTED, MutationState.UNCERTAIN}:
            return MutationExecutionResult(
                record=self._order(mutation.intent_id),
                mutation=mutation,
                mutation_attempted=False,
                recovered_by_read=False,
            )
        return self._recover(mutation, occurred_at=occurred_at)

    def _attempt(
        self,
        mutation: OrderMutationRecord,
        message: MutationOutboxMessage,
        *,
        occurred_at: datetime,
    ) -> MutationExecutionResult:
        try:
            if mutation.kind is MutationKind.CANCEL:
                broker_order = self.broker.cancel_order(
                    broker_order_id=mutation.broker_order_id
                )
            else:
                target = mutation.target_limit_price
                if target is None:
                    raise ValueError("REPLACE_TARGET_MISSING")
                broker_order = self.broker.replace_limit_order(
                    broker_order_id=mutation.broker_order_id,
                    limit_price=target,
                )
        except BrokerMutationError as exc:
            result = self._read_after_error(
                mutation,
                error_code=exc.code,
                occurred_at=occurred_at,
            )
            self.mutations.mark_outbox_published(message.message_id, occurred_at=occurred_at)
            return MutationExecutionResult(
                record=result.record,
                mutation=result.mutation,
                mutation_attempted=True,
                recovered_by_read=True,
            )

        result = self._resolve_truth(
            mutation,
            broker_order,
            occurred_at=occurred_at,
            ambiguous_context=False,
        )
        self.mutations.mark_outbox_published(message.message_id, occurred_at=occurred_at)
        return MutationExecutionResult(
            record=result.record,
            mutation=result.mutation,
            mutation_attempted=True,
            recovered_by_read=False,
        )

    def _read_after_error(
        self,
        mutation: OrderMutationRecord,
        *,
        error_code: str,
        occurred_at: datetime,
    ) -> MutationExecutionResult:
        order = self._order(mutation.intent_id)
        try:
            broker_order = self.broker.get_order_by_client_order_id(order.client_order_id)
        except BrokerMutationError:
            broker_order = None
        if broker_order is None:
            uncertain = self.mutations.mark_uncertain(
                mutation.mutation_id,
                outcome=f"UNRESOLVED_AFTER_{error_code}",
                occurred_at=occurred_at,
            )
            return MutationExecutionResult(order, uncertain, False, True)
        return self._resolve_truth(
            mutation,
            broker_order,
            occurred_at=occurred_at,
            ambiguous_context=True,
        )

    def _recover(
        self,
        mutation: OrderMutationRecord,
        *,
        occurred_at: datetime,
    ) -> MutationExecutionResult:
        order = self._order(mutation.intent_id)
        try:
            broker_order = self.broker.get_order_by_client_order_id(order.client_order_id)
        except BrokerMutationError:
            broker_order = None
        if broker_order is None:
            uncertain = self._mark_uncertain_if_needed(
                mutation,
                outcome="BROKER_ORDER_NOT_FOUND_DURING_RECOVERY",
                occurred_at=occurred_at,
            )
            return MutationExecutionResult(order, uncertain, False, True)
        result = self._resolve_truth(
            mutation,
            broker_order,
            occurred_at=occurred_at,
            ambiguous_context=True,
        )
        return MutationExecutionResult(
            record=result.record,
            mutation=result.mutation,
            mutation_attempted=False,
            recovered_by_read=True,
        )

    def _resolve_truth(
        self,
        mutation: OrderMutationRecord,
        broker_order: BrokerOrder,
        *,
        occurred_at: datetime,
        ambiguous_context: bool,
    ) -> MutationExecutionResult:
        order = self._validate_broker_truth(mutation.intent_id, broker_order)
        order = self._adopt_fill(order, broker_order, mutation, occurred_at=occurred_at)

        if broker_order.status is BrokerOrderStatus.FILLED:
            failed = self.mutations.mark_failed(
                mutation.mutation_id,
                outcome="ORDER_FILLED_BEFORE_MUTATION_COMPLETED",
                occurred_at=occurred_at,
            )
            return MutationExecutionResult(order, failed, False, ambiguous_context)

        if broker_order.status is BrokerOrderStatus.CANCELLED:
            if order.state is OrderState.FILLED:
                failed = self.mutations.mark_failed(
                    mutation.mutation_id,
                    outcome="CANCELLED_WITH_FULL_FILL",
                    occurred_at=occurred_at,
                )
                return MutationExecutionResult(order, failed, False, ambiguous_context)
            if order.state is not OrderState.CANCELLED:
                order = self.oms.transition(
                    order.intent_id,
                    OrderState.CANCELLED,
                    event_id=f"mutation:{mutation.mutation_id}:cancelled",
                    occurred_at=occurred_at,
                    broker_order_id=broker_order.broker_order_id,
                )
            if mutation.kind is MutationKind.CANCEL:
                succeeded = self.mutations.mark_succeeded(
                    mutation.mutation_id,
                    outcome="CANCELLED",
                    occurred_at=occurred_at,
                    broker_order_id=broker_order.broker_order_id,
                )
                return MutationExecutionResult(order, succeeded, False, ambiguous_context)
            failed = self.mutations.mark_failed(
                mutation.mutation_id,
                outcome="ORDER_CANCELLED_DURING_REPLACE",
                occurred_at=occurred_at,
            )
            return MutationExecutionResult(order, failed, False, ambiguous_context)

        if broker_order.status is BrokerOrderStatus.REJECTED:
            failed = self.mutations.mark_failed(
                mutation.mutation_id,
                outcome="BROKER_ORDER_REJECTED",
                occurred_at=occurred_at,
            )
            return MutationExecutionResult(order, failed, False, ambiguous_context)

        if mutation.kind is MutationKind.REPLACE:
            target = mutation.target_limit_price
            if target is None:
                raise ValueError("REPLACE_TARGET_MISSING")
            if broker_order.limit_price == target:
                succeeded = self.mutations.mark_succeeded(
                    mutation.mutation_id,
                    outcome="REPLACED",
                    occurred_at=occurred_at,
                    broker_order_id=broker_order.broker_order_id,
                )
                return MutationExecutionResult(order, succeeded, False, ambiguous_context)
            uncertain = self._mark_uncertain_if_needed(
                mutation,
                outcome="REPLACE_PRICE_NOT_CONFIRMED",
                occurred_at=occurred_at,
            )
            return MutationExecutionResult(order, uncertain, False, True)

        uncertain = self._mark_uncertain_if_needed(
            mutation,
            outcome="CANCEL_NOT_CONFIRMED",
            occurred_at=occurred_at,
        )
        return MutationExecutionResult(order, uncertain, False, True)

    def _adopt_fill(
        self,
        order: OrderRecord,
        broker_order: BrokerOrder,
        mutation: OrderMutationRecord,
        *,
        occurred_at: datetime,
    ) -> OrderRecord:
        if broker_order.filled_quantity <= order.filled_quantity:
            return order
        return self.oms.apply_cumulative_fill(
            order.intent_id,
            event_id=(
                f"mutation:{mutation.mutation_id}:fill:{broker_order.filled_quantity}"
            ),
            cumulative_filled=broker_order.filled_quantity,
            occurred_at=occurred_at,
            broker_order_id=broker_order.broker_order_id,
        )

    def _validate_broker_truth(self, intent_id: str, broker_order: BrokerOrder) -> OrderRecord:
        broker_order.validate()
        order = self._order(intent_id)
        if broker_order.client_order_id != order.client_order_id:
            raise ValueError("BROKER_CLIENT_ORDER_ID_MISMATCH")
        if broker_order.instrument != order.symbol:
            raise ValueError("BROKER_SYMBOL_MISMATCH")
        if broker_order.side.value != order.side.value:
            raise ValueError("BROKER_SIDE_MISMATCH")
        if broker_order.quantity != order.quantity:
            raise ValueError("BROKER_QUANTITY_MISMATCH")
        return order

    def _order(self, intent_id: str) -> OrderRecord:
        order = self.oms.get(intent_id)
        if order is None:
            raise KeyError(intent_id)
        return order

    def _mark_uncertain_if_needed(
        self,
        mutation: OrderMutationRecord,
        *,
        outcome: str,
        occurred_at: datetime,
    ) -> OrderMutationRecord:
        current = self.mutations.get(mutation.mutation_id)
        if current is None:
            raise KeyError(mutation.mutation_id)
        if current.state is MutationState.UNCERTAIN and current.outcome == outcome:
            return current
        return self.mutations.mark_uncertain(
            mutation.mutation_id,
            outcome=outcome,
            occurred_at=occurred_at,
        )
