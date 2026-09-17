from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.alpaca_fill_backfill import (
    AlpacaFillActivity,
    FillActivityPage,
    PaperFillBackfillService,
)
from app.execution.execution_facts import SQLiteExecutionFactStore
from app.execution.order_mutation_executor import PaperOrderMutationExecutor
from app.execution.trade_fills import ExplicitZeroPaperFeeModel, PaperTradeFillAccounting
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.order_mutations import (
    DurableOrderMutationStore,
    MutationState,
    OrderMutationLifecycle,
)
from app.oms.reconciliation import BrokerOrderState, BrokerOrderTruth, OmsReconciler
from app.oms.store import OrderState
from app.portfolio.strict import StrictPortfolioEventStore
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskDecision,
    RiskEvaluationMode,
    RiskLimits,
)
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus, OrderSide

NOW = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="f18-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f18-lineage",
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


def prepared(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "f18.sqlite"
    oms = IndexedDurableOmsStore(db)
    value = intent()
    PaperOrderLifecycle(oms).prepare(value, approved(value), occurred_at=NOW)
    oms.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="f18-submit-started",
        occurred_at=NOW,
    )
    oms.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="f18-ack",
        occurred_at=NOW,
        broker_order_id="broker-A",
    )
    mutations = DurableOrderMutationStore(db)
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    return db, oms, mutations, lifecycle


def prove_replace(
    oms: IndexedDurableOmsStore,
    mutations: DurableOrderMutationStore,
    lifecycle: OrderMutationLifecycle,
    *,
    mutation_id: str,
    target: str,
    successor: str,
) -> None:
    requested = lifecycle.request_replace(
        "f18-intent",
        mutation_id=mutation_id,
        target_limit_price=Decimal(target),
        occurred_at=NOW,
    )
    predecessor = requested.broker_order_id
    mutations.mark_started(mutation_id, occurred_at=NOW)
    mutations.mark_succeeded(
        mutation_id,
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id=successor,
    )
    oms.register_replace_successor(
        intent_id="f18-intent",
        mutation_id=mutation_id,
        predecessor_broker_order_id=predecessor,
        successor_broker_order_id=successor,
        occurred_at=NOW,
    )


def test_unproven_broker_successor_is_rejected(tmp_path) -> None:
    _, oms, _, _ = prepared(tmp_path)

    with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
        oms.register_replace_successor(
            intent_id="f18-intent",
            mutation_id="missing-replace",
            predecessor_broker_order_id="broker-A",
            successor_broker_order_id="broker-B",
            occurred_at=NOW,
        )

    assert oms.get_by_broker_order_id("broker-B") is None


def test_successful_replace_builds_idempotent_multi_generation_lineage(tmp_path) -> None:
    db, oms, mutations, lifecycle = prepared(tmp_path)
    prove_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-A-B",
        target="101",
        successor="broker-B",
    )
    prove_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-B-C",
        target="102",
        successor="broker-C",
    )

    for broker_id in ("broker-A", "broker-B", "broker-C"):
        resolved = oms.get_by_broker_order_id(broker_id)
        assert resolved is not None
        assert resolved.intent_id == "f18-intent"

    oms.register_replace_successor(
        intent_id="f18-intent",
        mutation_id="replace-A-B",
        predecessor_broker_order_id="broker-A",
        successor_broker_order_id="broker-B",
        occurred_at=NOW + timedelta(seconds=1),
    )

    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            """SELECT broker_order_id, predecessor_broker_order_id,
                      replace_mutation_id, generation
            FROM oms_broker_order_identities
            WHERE intent_id='f18-intent' ORDER BY generation"""
        ).fetchall()
        assert rows == [
            ("broker-A", None, None, 0),
            ("broker-B", "broker-A", "replace-A-B", 1),
            ("broker-C", "broker-B", "replace-B-C", 2),
        ]


def test_lineage_registry_is_append_only(tmp_path) -> None:
    db, oms, mutations, lifecycle = prepared(tmp_path)
    prove_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-A-B",
        target="101",
        successor="broker-B",
    )

    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """UPDATE oms_broker_order_identities
                SET broker_order_id='tampered' WHERE broker_order_id='broker-B'"""
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM oms_broker_order_identities WHERE broker_order_id='broker-B'"
            )


def test_reconciliation_rejects_drift_and_accepts_proven_successor(tmp_path) -> None:
    _, oms, mutations, lifecycle = prepared(tmp_path)
    local = oms.get("f18-intent")
    assert local is not None

    unproven = BrokerOrderTruth(
        client_order_id=local.client_order_id,
        broker_order_id="broker-D",
        state=BrokerOrderState.OPEN,
        cumulative_filled=Decimal("0"),
    )
    with pytest.raises(ValueError, match="BROKER_ORDER_ID_DRIFT"):
        OmsReconciler(oms).reconcile_order(
            "f18-intent",
            unproven,
            occurred_at=NOW,
            event_prefix="unproven-drift",
        )
    assert oms.get("f18-intent").broker_order_id == "broker-A"  # type: ignore[union-attr]

    prove_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-A-B",
        target="101",
        successor="broker-B",
    )
    proven = replace(unproven, broker_order_id="broker-B")
    result = OmsReconciler(oms).reconcile_order(
        "f18-intent",
        proven,
        occurred_at=NOW,
        event_prefix="proven-successor",
    )
    assert result.state is OrderState.ACKNOWLEDGED


class MutationBroker:
    paper_order_writes_enabled = True

    def __init__(self, *, client_order_id: str, cancel_drift: bool = False) -> None:
        self.cancel_drift = cancel_drift
        self.replace_calls = 0
        self.cancel_calls = 0
        self.order = BrokerOrder(
            client_order_id=client_order_id,
            broker_order_id="broker-A",
            instrument="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("2"),
            limit_price=Decimal("100"),
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )

    def replace_limit_order(self, *, broker_order_id: str, limit_price: Decimal) -> BrokerOrder:
        assert broker_order_id == self.order.broker_order_id
        self.replace_calls += 1
        self.order = replace(
            self.order,
            broker_order_id="broker-B",
            limit_price=limit_price,
            status=BrokerOrderStatus.REPLACED,
        )
        return self.order

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder:
        self.cancel_calls += 1
        returned_id = "broker-D" if self.cancel_drift else broker_order_id
        self.order = replace(
            self.order,
            broker_order_id=returned_id,
            status=BrokerOrderStatus.CANCELLED,
        )
        return self.order

    def get_order_by_client_order_id(self, client_order_id: str):
        return self.order if client_order_id == self.order.client_order_id else None


def test_executor_registers_successor_only_after_successful_replace(tmp_path) -> None:
    _, oms, mutations, lifecycle = prepared(tmp_path)
    local = oms.get("f18-intent")
    assert local is not None
    broker = MutationBroker(client_order_id=local.client_order_id)
    lifecycle.request_replace(
        "f18-intent",
        mutation_id="replace-executor",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    message = mutations.pending_outbox()[0]

    result = PaperOrderMutationExecutor(
        oms=oms,
        mutations=mutations,
        broker=broker,
    ).execute(message, occurred_at=NOW)

    assert result.mutation.state is MutationState.SUCCEEDED
    assert result.mutation.broker_order_id == "broker-B"
    assert broker.replace_calls == 1
    assert oms.get_by_broker_order_id("broker-A").intent_id == "f18-intent"  # type: ignore[union-attr]
    assert oms.get_by_broker_order_id("broker-B").intent_id == "f18-intent"  # type: ignore[union-attr]


def test_cancel_response_with_unproven_new_id_is_quarantined_without_rewrite(tmp_path) -> None:
    _, oms, mutations, lifecycle = prepared(tmp_path)
    local = oms.get("f18-intent")
    assert local is not None
    broker = MutationBroker(client_order_id=local.client_order_id, cancel_drift=True)
    lifecycle.request_cancel(
        "f18-intent",
        mutation_id="cancel-drift",
        occurred_at=NOW,
    )

    result = PaperOrderMutationExecutor(
        oms=oms,
        mutations=mutations,
        broker=broker,
    ).execute(mutations.pending_outbox()[0], occurred_at=NOW)

    assert result.mutation.state is MutationState.UNCERTAIN
    assert result.mutation.outcome == "BROKER_ORDER_ID_DRIFT"
    assert result.record.broker_order_id == "broker-A"
    assert broker.cancel_calls == 1


def test_fill_backfill_maps_predecessor_and_successor_to_same_order(tmp_path) -> None:
    _, oms, mutations, lifecycle = prepared(tmp_path)
    prove_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-A-B",
        target="101",
        successor="broker-B",
    )

    class Source:
        def page(self, *, after, until, page_size, page_token):
            assert page_token is None
            return FillActivityPage(
                (
                    AlpacaFillActivity(
                        activity_id="fill-A",
                        broker_order_id="broker-A",
                        symbol="AAPL",
                        side=Side.BUY,
                        cumulative_quantity=Decimal("1"),
                        quantity=Decimal("1"),
                        price=Decimal("100"),
                        occurred_at=NOW + timedelta(seconds=1),
                        activity_kind="fill",
                    ),
                    AlpacaFillActivity(
                        activity_id="fill-B",
                        broker_order_id="broker-B",
                        symbol="AAPL",
                        side=Side.BUY,
                        cumulative_quantity=Decimal("2"),
                        quantity=Decimal("1"),
                        price=Decimal("101"),
                        occurred_at=NOW + timedelta(seconds=2),
                        activity_kind="fill",
                    ),
                ),
                None,
            )

    portfolio = StrictPortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        execution_facts=SQLiteExecutionFactStore(tmp_path / "execution.sqlite"),
        opening_cash=Decimal("1000"),
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=ledger,
    )
    service = PaperFillBackfillService(source=Source(), oms=oms, accounting=accounting)

    recovered = service.recover(after=NOW, until=NOW + timedelta(minutes=1))

    assert recovered.complete
    assert recovered.unresolved_broker_order_ids == ()
    assert recovered.activities_seen == 2
    assert oms.get("f18-intent").state is OrderState.FILLED  # type: ignore[union-attr]
    assert ledger.position("AAPL").quantity == Decimal("2")
    assert ledger.cash == Decimal("799")
