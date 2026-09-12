from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.paper_executor import PaperSubmitExecutor
from app.oms.store import DurableOmsStore, OrderState
from app.risk.pretrade import RiskDecision
from app.runtime.paper_broker_contract_v99 import (
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 15, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="exec-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="execution-validation",
    )


def approved() -> RiskDecision:
    return RiskDecision(True, (), Decimal("1000"), Decimal("1000"), Decimal("1000"))


class FakePaperBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.get_calls = 0
        self.orders: dict[str, BrokerOrder] = {}
        self.ambiguous_submit = False
        self.persist_ambiguous_order = True
        self.submit_status = BrokerOrderStatus.ACKNOWLEDGED
        self.submit_filled = Decimal("0")

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-exec-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=self.submit_status,
            filled_quantity=self.submit_filled,
            updated_at=NOW,
        )
        if not self.ambiguous_submit or self.persist_ambiguous_order:
            self.orders[order.client_order_id] = order
        if self.ambiguous_submit:
            raise BrokerMutationError("TIMEOUT", "submit outcome ambiguous", ambiguous=True)
        return order

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.orders.get(client_order_id)


class BlockingPaperBroker(FakePaperBroker):
    def __init__(self) -> None:
        super().__init__()
        self.submit_entered = Event()
        self.release_submit = Event()

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_entered.set()
        if not self.release_submit.wait(timeout=5):
            raise RuntimeError("test submit release timed out")
        return super().submit_limit_order(**kwargs)


def prepared_store(tmp_path):
    store = DurableOmsStore(tmp_path / "executor.sqlite")
    PaperOrderLifecycle(store).prepare(intent(), approved(), occurred_at=NOW)
    message = store.pending_outbox()[0]
    return store, message


def test_normal_submit_is_attempted_once_and_persisted(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    broker = FakePaperBroker()
    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert result.record.state is OrderState.ACKNOWLEDGED
    assert result.mutation_attempted
    assert not result.recovered_by_read
    assert broker.submit_calls == 1
    assert store.pending_outbox() == ()
    assert store.get("exec-intent").broker_order_id == "broker-exec-1"  # type: ignore[union-attr]


def test_ambiguous_submit_is_recovered_by_get_without_second_post(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    broker = FakePaperBroker()
    broker.ambiguous_submit = True
    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert result.record.state is OrderState.ACKNOWLEDGED
    assert result.recovered_by_read
    assert broker.submit_calls == 1
    PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert broker.submit_calls == 1


def test_unresolved_ambiguous_submit_enters_uncertain_and_never_retries(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    broker = FakePaperBroker()
    broker.ambiguous_submit = True
    broker.persist_ambiguous_order = False
    first = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert first.record.state is OrderState.UNCERTAIN
    assert first.recovered_by_read
    assert broker.submit_calls == 1
    second = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert second.record.state is OrderState.UNCERTAIN
    assert broker.submit_calls == 1


def test_restart_after_submit_started_uses_get_only_after_recovery_grace(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    store.transition(
        "exec-intent",
        OrderState.SUBMIT_STARTED,
        event_id="simulated-crash-after-start-marker",
        occurred_at=NOW,
    )
    broker = FakePaperBroker()
    result = PaperSubmitExecutor(store=store, broker=broker).execute(
        message,
        occurred_at=NOW + timedelta(seconds=31),
    )
    assert result.record.state is OrderState.UNCERTAIN
    assert result.recovered_by_read
    assert not result.mutation_attempted
    assert broker.submit_calls == 0
    assert broker.get_calls == 1


def test_fresh_submit_started_is_not_recovered_while_owner_may_be_in_network_call(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    store.transition(
        "exec-intent",
        OrderState.SUBMIT_STARTED,
        event_id="active-owner",
        occurred_at=NOW,
    )
    broker = FakePaperBroker()
    result = PaperSubmitExecutor(store=store, broker=broker).execute(
        message,
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert result.record.state is OrderState.SUBMIT_STARTED
    assert not result.mutation_attempted
    assert not result.recovered_by_read
    assert broker.submit_calls == 0
    assert broker.get_calls == 0
    assert len(store.pending_outbox()) == 1


def test_two_workers_share_one_submit_claim_and_only_winner_can_post(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    broker = BlockingPaperBroker()
    winner = PaperSubmitExecutor(store=store, broker=broker)
    contender = PaperSubmitExecutor(store=store, broker=broker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(winner.execute, message, occurred_at=NOW)
        assert broker.submit_entered.wait(timeout=5)
        second = pool.submit(
            contender.execute,
            message,
            occurred_at=NOW + timedelta(seconds=1),
        )
        second_result = second.result(timeout=5)
        assert second_result.record.state is OrderState.SUBMIT_STARTED
        assert not second_result.mutation_attempted
        assert not second_result.recovered_by_read
        assert broker.submit_calls == 0
        assert broker.get_calls == 0

        broker.release_submit.set()
        first_result = first.result(timeout=5)

    assert first_result.record.state is OrderState.ACKNOWLEDGED
    assert first_result.mutation_attempted
    assert broker.submit_calls == 1
    assert broker.get_calls == 0
    assert store.pending_outbox() == ()


def test_submit_truth_can_adopt_partial_fill_monotonically(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    broker = FakePaperBroker()
    broker.submit_status = BrokerOrderStatus.PARTIALLY_FILLED
    broker.submit_filled = Decimal("4")
    result = PaperSubmitExecutor(store=store, broker=broker).execute(message, occurred_at=NOW)
    assert result.record.state is OrderState.PARTIALLY_FILLED
    assert result.record.filled_quantity == Decimal("4")
    assert broker.submit_calls == 1


def test_restart_can_adopt_existing_broker_order_without_post(tmp_path) -> None:
    store, message = prepared_store(tmp_path)
    store.transition("exec-intent", OrderState.SUBMIT_STARTED, event_id="started", occurred_at=NOW)
    broker = FakePaperBroker()
    client_order_id = store.get("exec-intent").client_order_id  # type: ignore[union-attr]
    seed = BrokerOrder(
        client_order_id=client_order_id,
        broker_order_id="broker-existing",
        instrument="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        status=BrokerOrderStatus.ACKNOWLEDGED,
        filled_quantity=Decimal("0"),
        updated_at=NOW,
    )
    broker.orders[client_order_id] = replace(seed)
    result = PaperSubmitExecutor(store=store, broker=broker).execute(
        message,
        occurred_at=NOW + timedelta(seconds=31),
    )
    assert result.record.state is OrderState.ACKNOWLEDGED
    assert result.record.broker_order_id == "broker-existing"
    assert broker.submit_calls == 0
