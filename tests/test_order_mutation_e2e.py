from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.order_mutation_executor import PaperOrderMutationExecutor
from app.execution.paper_executor import PaperSubmitExecutor
from app.oms.order_mutations import (
    ActiveMutationExists,
    DurableOrderMutationStore,
    MutationState,
    OrderMutationLifecycle,
)
from app.oms.store import DurableOmsStore, OrderState
from app.risk.pretrade import RiskDecision
from app.runtime.paper_broker_contract_v99 import (
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
)

NOW = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="mutation-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="mutation-e2e",
    )


def approved() -> RiskDecision:
    return RiskDecision(True, (), Decimal("1000"), Decimal("1000"), Decimal("1000"))


class FakePaperBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.cancel_calls = 0
        self.replace_calls = 0
        self.cancel_ambiguous = False
        self.cancel_persists_before_error = True
        self.replace_ambiguous = False
        self.replace_persists_before_error = True
        self.replace_filled = Decimal("0")
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-mut-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        return order

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder:
        self.cancel_calls += 1
        current = self._by_broker_id(broker_order_id)
        cancelled = replace(current, status=BrokerOrderStatus.CANCELLED, updated_at=NOW)
        if not self.cancel_ambiguous or self.cancel_persists_before_error:
            self.orders[cancelled.client_order_id] = cancelled
        if self.cancel_ambiguous:
            raise BrokerMutationError("TIMEOUT", "cancel outcome ambiguous", ambiguous=True)
        return cancelled

    def replace_limit_order(
        self, *, broker_order_id: str, limit_price: Decimal
    ) -> BrokerOrder:
        self.replace_calls += 1
        current = self._by_broker_id(broker_order_id)
        status = (
            BrokerOrderStatus.PARTIALLY_FILLED
            if self.replace_filled > 0
            else BrokerOrderStatus.REPLACED
        )
        replaced = replace(
            current,
            broker_order_id=f"broker-mut-{self.replace_calls + 1}",
            limit_price=limit_price,
            status=status,
            filled_quantity=self.replace_filled,
            updated_at=NOW,
        )
        if not self.replace_ambiguous or self.replace_persists_before_error:
            self.orders[replaced.client_order_id] = replaced
        if self.replace_ambiguous:
            raise BrokerMutationError("TIMEOUT", "replace outcome ambiguous", ambiguous=True)
        return replaced

    def get_order_by_client_order_id(self, client_order_id: str):
        return self.orders.get(client_order_id)

    def _by_broker_id(self, broker_order_id: str) -> BrokerOrder:
        for order in self.orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        raise BrokerMutationError("404", "order not found", ambiguous=False)


def prepared(tmp_path):
    db = tmp_path / "order-mutations.sqlite"
    oms = DurableOmsStore(db)
    PaperOrderLifecycle(oms).prepare(intent(), approved(), occurred_at=NOW)
    broker = FakePaperBroker()
    submit_message = oms.pending_outbox()[0]
    submit = PaperSubmitExecutor(store=oms, broker=broker).execute(
        submit_message, occurred_at=NOW
    )
    assert submit.record.state is OrderState.ACKNOWLEDGED
    mutations = DurableOrderMutationStore(db)
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    executor = PaperOrderMutationExecutor(oms=oms, mutations=mutations, broker=broker)
    return db, oms, mutations, lifecycle, executor, broker


def test_cancel_is_single_attempt_and_durable(tmp_path) -> None:
    _, oms, mutations, lifecycle, executor, broker = prepared(tmp_path)
    requested = lifecycle.request_cancel(
        "mutation-intent", mutation_id="cancel-1", occurred_at=NOW
    )
    assert requested.state is MutationState.REQUESTED
    message = mutations.pending_outbox()[0]
    result = executor.execute(message, occurred_at=NOW)
    assert result.mutation.state is MutationState.SUCCEEDED
    assert result.mutation.outcome == "CANCELLED"
    assert result.record.state is OrderState.CANCELLED
    assert result.mutation_attempted
    assert broker.cancel_calls == 1
    assert mutations.pending_outbox() == ()
    assert oms.get("mutation-intent").state is OrderState.CANCELLED  # type: ignore[union-attr]


def test_ambiguous_cancel_is_recovered_by_get_without_second_delete(tmp_path) -> None:
    _, _, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_cancel("mutation-intent", mutation_id="cancel-amb", occurred_at=NOW)
    broker.cancel_ambiguous = True
    message = mutations.pending_outbox()[0]
    result = executor.execute(message, occurred_at=NOW)
    assert result.mutation.state is MutationState.SUCCEEDED
    assert result.recovered_by_read
    assert broker.cancel_calls == 1
    executor.execute(message, occurred_at=NOW)
    assert broker.cancel_calls == 1


def test_restart_after_cancel_started_is_get_only_then_reconcilable(tmp_path) -> None:
    _, _, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_cancel("mutation-intent", mutation_id="cancel-crash", occurred_at=NOW)
    message = mutations.pending_outbox()[0]
    mutations.mark_started("cancel-crash", occurred_at=NOW)
    first = executor.execute(message, occurred_at=NOW)
    assert first.mutation.state is MutationState.UNCERTAIN
    assert first.recovered_by_read
    assert not first.mutation_attempted
    assert broker.cancel_calls == 0

    current = broker.orders[first.record.client_order_id]
    broker.orders[current.client_order_id] = replace(
        current, status=BrokerOrderStatus.CANCELLED
    )
    resolved = executor.reconcile("cancel-crash", occurred_at=NOW)
    assert resolved.mutation.state is MutationState.SUCCEEDED
    assert resolved.record.state is OrderState.CANCELLED
    assert broker.cancel_calls == 0


def test_replace_tracks_effective_price_and_rotated_broker_id(tmp_path) -> None:
    _, _, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_replace(
        "mutation-intent",
        mutation_id="replace-1",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    result = executor.execute(mutations.pending_outbox()[0], occurred_at=NOW)
    assert result.mutation.state is MutationState.SUCCEEDED
    assert result.mutation.outcome == "REPLACED"
    assert result.mutation.broker_order_id == "broker-mut-2"
    assert mutations.current_limit_price(
        "mutation-intent", fallback=Decimal("100")
    ) == Decimal("101")
    assert mutations.current_broker_order_id(
        "mutation-intent", fallback="broker-mut-1"
    ) == "broker-mut-2"

    cancel = lifecycle.request_cancel(
        "mutation-intent", mutation_id="cancel-after-replace", occurred_at=NOW
    )
    assert cancel.broker_order_id == "broker-mut-2"
    assert broker.replace_calls == 1


def test_ambiguous_replace_is_recovered_by_get_without_second_patch(tmp_path) -> None:
    _, _, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_replace(
        "mutation-intent",
        mutation_id="replace-amb",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    broker.replace_ambiguous = True
    result = executor.execute(mutations.pending_outbox()[0], occurred_at=NOW)
    assert result.mutation.state is MutationState.SUCCEEDED
    assert result.recovered_by_read
    assert broker.replace_calls == 1


def test_restart_after_replace_started_never_patches_blindly(tmp_path) -> None:
    _, _, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_replace(
        "mutation-intent",
        mutation_id="replace-crash",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    message = mutations.pending_outbox()[0]
    mutations.mark_started("replace-crash", occurred_at=NOW)
    result = executor.execute(message, occurred_at=NOW)
    assert result.mutation.state is MutationState.UNCERTAIN
    assert result.mutation.outcome == "REPLACE_PRICE_NOT_CONFIRMED"
    assert broker.replace_calls == 0


def test_replace_adopts_partial_fill_before_success(tmp_path) -> None:
    _, oms, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_replace(
        "mutation-intent",
        mutation_id="replace-fill",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    broker.replace_filled = Decimal("3")
    result = executor.execute(mutations.pending_outbox()[0], occurred_at=NOW)
    assert result.mutation.state is MutationState.SUCCEEDED
    assert result.record.state is OrderState.PARTIALLY_FILLED
    assert result.record.filled_quantity == Decimal("3")
    assert oms.get("mutation-intent").filled_quantity == Decimal("3")  # type: ignore[union-attr]


def test_active_mutation_fence_and_request_idempotency(tmp_path) -> None:
    _, _, _, lifecycle, _, _ = prepared(tmp_path)
    first = lifecycle.request_replace(
        "mutation-intent",
        mutation_id="replace-fence",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    repeated = lifecycle.request_replace(
        "mutation-intent",
        mutation_id="replace-fence",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    assert repeated == first
    with pytest.raises(ActiveMutationExists, match="ACTIVE_MUTATION_EXISTS"):
        lifecycle.request_cancel(
            "mutation-intent", mutation_id="cancel-blocked", occurred_at=NOW
        )


def test_mutation_event_journal_is_append_only(tmp_path) -> None:
    db, _, mutations, lifecycle, _, _ = prepared(tmp_path)
    lifecycle.request_cancel("mutation-intent", mutation_id="cancel-journal", occurred_at=NOW)
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE oms_order_mutation_events SET event_type='TAMPERED'"
            )
    assert mutations.events("cancel-journal")[0]["event_type"] == "REQUESTED"


def test_writes_disabled_fails_before_started_marker(tmp_path) -> None:
    _, _, mutations, lifecycle, executor, broker = prepared(tmp_path)
    lifecycle.request_cancel("mutation-intent", mutation_id="cancel-disabled", occurred_at=NOW)
    broker.paper_order_writes_enabled = False
    with pytest.raises(ValueError, match="PAPER_ORDER_WRITES_DISABLED"):
        executor.execute(mutations.pending_outbox()[0], occurred_at=NOW)
    persisted = mutations.get("cancel-disabled")
    assert persisted is not None and persisted.state is MutationState.REQUESTED
    assert broker.cancel_calls == 0
