from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_protection_reprice import (
    PaperProtectionRepricePlanner,
    ProtectiveRepriceStatus,
    ProtectiveSellRepricePolicy,
)
from app.domain.trading import OrderIntent, Side
from app.oms.order_mutations import (
    DurableOrderMutationStore,
    MutationState,
    OrderMutationLifecycle,
)
from app.oms.store import DurableOmsStore, OrderState

NOW = datetime(2026, 8, 12, 0, 30, tzinfo=UTC)


def acknowledged_sell(path: Path):
    oms = DurableOmsStore(path)
    intent = OrderIntent(
        intent_id="protective-intent",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow:protection",
    )
    oms.create(intent, client_order_id="protective-client", occurred_at=NOW)
    oms.approve_risk(intent.intent_id, event_id="risk", occurred_at=NOW)
    oms.enqueue_submit(intent.intent_id, event_id="outbox", occurred_at=NOW)
    oms.transition(
        intent.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit",
        occurred_at=NOW,
    )
    oms.transition(
        intent.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="ack",
        occurred_at=NOW,
        broker_order_id="broker-1",
    )
    return oms, intent


def planner(path: Path, oms: DurableOmsStore) -> PaperProtectionRepricePlanner:
    mutations = DurableOrderMutationStore(path)
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    return PaperProtectionRepricePlanner(
        oms=oms,
        mutation_lifecycle=lifecycle,
        mutations=mutations,
        policy=ProtectiveSellRepricePolicy(
            minimum_adverse_move_fraction=Decimal("0.0025"),
            marketability_cushion_fraction=Decimal("0"),
        ),
    )


def test_adverse_move_requests_one_durable_replace_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oms.sqlite"
    oms, intent = acknowledged_sell(path)
    reprice = planner(path, oms)

    first = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert first.status is ProtectiveRepriceStatus.REQUESTED
    assert first.current_limit_price == Decimal("101")
    assert first.target_limit_price == Decimal("99")
    assert first.mutation is not None
    assert first.mutation.state is MutationState.REQUESTED
    pending = reprice.mutations.pending_outbox()
    assert len(pending) == 1
    assert pending[0].topic == "paper_order_replace"

    retry = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=2),
    )
    assert retry.status is ProtectiveRepriceStatus.REQUESTED
    assert retry.mutation is not None
    assert retry.mutation.mutation_id == first.mutation.mutation_id
    assert len(reprice.mutations.pending_outbox()) == 1


def test_new_lower_price_waits_while_prior_replace_is_active(tmp_path: Path) -> None:
    path = tmp_path / "oms.sqlite"
    oms, intent = acknowledged_sell(path)
    reprice = planner(path, oms)
    first = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert first.mutation is not None

    blocked = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("98"),
        observed_at=NOW + timedelta(seconds=2),
    )
    assert blocked.status is ProtectiveRepriceStatus.ACTIVE_MUTATION_EXISTS
    assert len(reprice.mutations.pending_outbox()) == 1

    reprice.mutations.mark_started(
        first.mutation.mutation_id,
        occurred_at=NOW + timedelta(seconds=3),
    )
    reprice.mutations.mark_succeeded(
        first.mutation.mutation_id,
        outcome="PATCH_CONFIRMED",
        occurred_at=NOW + timedelta(seconds=4),
        broker_order_id="broker-1-replaced",
    )
    second = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("98"),
        observed_at=NOW + timedelta(seconds=5),
    )
    assert second.status is ProtectiveRepriceStatus.REQUESTED
    assert second.current_limit_price == Decimal("99")
    assert second.target_limit_price == Decimal("98")
    assert second.mutation is not None
    assert second.mutation.mutation_id != first.mutation.mutation_id


def test_improving_or_tiny_move_does_not_chase_sell_limit(tmp_path: Path) -> None:
    path = tmp_path / "oms.sqlite"
    oms, intent = acknowledged_sell(path)
    reprice = planner(path, oms)

    improving = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("102"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert improving.status is ProtectiveRepriceStatus.NOT_NEEDED

    tiny = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("100.90"),
        observed_at=NOW + timedelta(seconds=2),
    )
    assert tiny.status is ProtectiveRepriceStatus.NOT_NEEDED
    assert reprice.mutations.pending_outbox() == ()


def test_preack_and_terminal_orders_are_not_mutated(tmp_path: Path) -> None:
    path = tmp_path / "oms.sqlite"
    oms = DurableOmsStore(path)
    intent = OrderIntent(
        intent_id="waiting-intent",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow:protection",
    )
    oms.create(intent, client_order_id="waiting-client", occurred_at=NOW)
    reprice = planner(path, oms)

    waiting = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert waiting.status is ProtectiveRepriceStatus.WAITING_FOR_ACTIVE_ORDER

    oms.transition(
        intent.intent_id,
        OrderState.REJECTED,
        event_id="rejected",
        occurred_at=NOW + timedelta(seconds=2),
    )
    terminal = reprice.consider(
        intent_id=intent.intent_id,
        reference_price=Decimal("98"),
        observed_at=NOW + timedelta(seconds=3),
    )
    assert terminal.status is ProtectiveRepriceStatus.TERMINAL_ORDER
    assert reprice.mutations.pending_outbox() == ()
