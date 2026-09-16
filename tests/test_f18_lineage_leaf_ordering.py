from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.order_mutations import DurableOrderMutationStore, OrderMutationLifecycle
from app.oms.store import OrderState
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskEvaluationMode,
    RiskLimits,
)

NOW = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="leaf-order-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f18-leaf-order",
    )


def _approved(value: OrderIntent):
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


def _prove_replace(
    *,
    oms: IndexedDurableOmsStore,
    mutations: DurableOrderMutationStore,
    lifecycle: OrderMutationLifecycle,
    mutation_id: str,
    target: str,
    successor: str,
) -> None:
    requested = lifecycle.request_replace(
        "leaf-order-intent",
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
        intent_id="leaf-order-intent",
        mutation_id=mutation_id,
        predecessor_broker_order_id=predecessor,
        successor_broker_order_id=successor,
        occurred_at=NOW,
    )


def test_lineage_generation_beats_equal_timestamp_and_reverse_mutation_id_order(tmp_path) -> None:
    db = tmp_path / "leaf-order.sqlite"
    oms = IndexedDurableOmsStore(db)
    value = _intent()
    PaperOrderLifecycle(oms).prepare(value, _approved(value), occurred_at=NOW)
    oms.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="leaf-submit",
        occurred_at=NOW,
    )
    oms.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="leaf-ack",
        occurred_at=NOW,
        broker_order_id="broker-A",
    )

    mutations = DurableOrderMutationStore(db)
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)

    # Lexically high id is first, lexically low id is second. Both use the exact
    # same timestamp. A timestamp/id ordering heuristic would therefore pick the
    # wrong predecessor after the second replace.
    _prove_replace(
        oms=oms,
        mutations=mutations,
        lifecycle=lifecycle,
        mutation_id="z-first",
        target="101",
        successor="broker-B",
    )
    _prove_replace(
        oms=oms,
        mutations=mutations,
        lifecycle=lifecycle,
        mutation_id="a-second",
        target="102",
        successor="broker-C",
    )

    third = lifecycle.request_replace(
        value.intent_id,
        mutation_id="m-third",
        target_limit_price=Decimal("103"),
        occurred_at=NOW,
    )

    assert third.broker_order_id == "broker-C"
    assert third.baseline_limit_price == Decimal("102")
