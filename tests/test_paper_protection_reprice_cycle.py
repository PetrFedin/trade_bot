from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.order_intents import order_intent_for_target
from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.paper_protection import (
    PaperProtectionService,
    PaperProtectionStatus,
    SQLitePaperProtectionStore,
)
from app.application.paper_protection_cycle import PaperProtectionOrderService
from app.application.paper_protection_reprice import (
    PaperProtectionRepricePlanner,
    ProtectiveRepriceStatus,
)
from app.application.portfolio_paper_planner import PortfolioPaperPlanner
from app.domain.trading import Fill, Side
from app.oms.order_mutations import DurableOrderMutationStore, OrderMutationLifecycle
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.position_management import PositionManagementPolicy

NOW = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)


def policy() -> PositionManagementPolicy:
    return PositionManagementPolicy(
        stop_loss_fraction=Decimal("0.02"),
        take_profit_fraction=Decimal("0.10"),
        trailing_activation_fraction=Decimal("0.08"),
        trailing_stop_fraction=Decimal("0.015"),
        maximum_holding_bars=10,
        break_even_activation_fraction=Decimal("0.01"),
        break_even_buffer_fraction=Decimal("0.001"),
        profit_protection_activation_fraction=Decimal("0.015"),
        maximum_profit_giveback_fraction=Decimal("0.50"),
    )


def risk_engine() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        )
    )


def seed_long(ledger: PortfolioLedger, *, quantity: str = "1") -> None:
    ledger.apply_fill(
        Fill(
            fill_id="seed-aapl",
            order_intent_id="seed-intent-aapl",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal(quantity),
            price=Decimal("100"),
            occurred_at=NOW - timedelta(minutes=1),
        )
    )


def build_service(tmp_path: Path, ledger: PortfolioLedger):
    oms_path = tmp_path / "protection-reprice-oms.sqlite"
    oms = DurableOmsStore(oms_path)
    mutations = DurableOrderMutationStore(oms_path)
    mutation_lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    reprice = PaperProtectionRepricePlanner(
        oms=oms,
        mutation_lifecycle=mutation_lifecycle,
        mutations=mutations,
    )
    protection = PaperProtectionService(
        ledger=ledger,
        store=SQLitePaperProtectionStore(tmp_path / "protection-state.sqlite"),
        policy=policy(),
    )
    lifecycle = PaperOrderLifecycle(oms)
    service = PaperProtectionOrderService(
        protection=protection,
        planner=PortfolioPaperPlanner(ledger=ledger, risk=risk_engine()),
        lifecycle=lifecycle,
        reprice_planner=reprice,
    )
    return service, oms, mutations


def arm_and_trigger(
    service: PaperProtectionOrderService,
):
    armed = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
        mark_prices={"AAPL": Decimal("102")},
    )
    assert armed.protection.status is PaperProtectionStatus.TRACKING
    triggered = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
    )
    assert triggered.prepared is not None
    return triggered


def acknowledge(
    oms: DurableOmsStore,
    *,
    intent_id: str,
    broker_order_id: str = "broker-protection-1",
) -> None:
    oms.transition(
        intent_id,
        OrderState.SUBMIT_STARTED,
        event_id=f"submit:{intent_id}",
        occurred_at=NOW + timedelta(seconds=2),
    )
    oms.transition(
        intent_id,
        OrderState.ACKNOWLEDGED,
        event_id=f"ack:{intent_id}",
        occurred_at=NOW + timedelta(seconds=3),
        broker_order_id=broker_order_id,
    )


def test_acknowledged_protective_sell_reprices_without_second_sell(
    tmp_path: Path,
) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms, mutations = build_service(tmp_path, ledger)
    first = arm_and_trigger(service)
    assert first.prepared is not None
    original_intent_id = first.prepared.record.intent_id
    acknowledge(oms, intent_id=original_intent_id)

    adverse = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=4),
        mark_prices={"AAPL": Decimal("99")},
    )

    assert adverse.existing_order_reused is True
    assert adverse.prepared is not None
    assert adverse.prepared.record.intent_id == original_intent_id
    assert adverse.reprice is not None
    assert adverse.reprice.status is ProtectiveRepriceStatus.REQUESTED
    assert adverse.reprice.current_limit_price == Decimal("101")
    assert adverse.reprice.target_limit_price == Decimal("99")
    assert len(mutations.pending_outbox()) == 1
    assert len(oms.pending_outbox()) == 1


def test_partial_fill_then_cancel_reissues_only_verified_remainder(tmp_path: Path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms, _ = build_service(tmp_path, ledger)
    first = arm_and_trigger(service)
    assert first.prepared is not None
    original_intent_id = first.prepared.record.intent_id
    acknowledge(oms, intent_id=original_intent_id)

    oms.apply_cumulative_fill(
        original_intent_id,
        event_id="partial:original",
        cumulative_filled=Decimal("0.4"),
        occurred_at=NOW + timedelta(seconds=4),
        broker_order_id="broker-protection-1",
    )
    ledger.apply_fill(
        Fill(
            fill_id="partial-fill",
            order_intent_id=original_intent_id,
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("0.4"),
            price=Decimal("100"),
            occurred_at=NOW + timedelta(seconds=4),
        )
    )
    oms.transition(
        original_intent_id,
        OrderState.CANCEL_REQUESTED,
        event_id="cancel-requested:original",
        occurred_at=NOW + timedelta(seconds=5),
    )
    oms.transition(
        original_intent_id,
        OrderState.CANCELLED,
        event_id="cancelled:original",
        occurred_at=NOW + timedelta(seconds=6),
    )
    assert ledger.position("AAPL").quantity == Decimal("0.6")

    replacement = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("98"),
        observed_at=NOW + timedelta(seconds=7),
        mark_prices={"AAPL": Decimal("98")},
    )

    assert replacement.existing_order_reused is False
    assert replacement.prepared is not None
    assert replacement.prepared.record.intent_id != original_intent_id
    assert replacement.prepared.record.quantity == Decimal("0.6")
    assert replacement.prepared.record.limit_price == Decimal("98")
    assert replacement.prepared.record.state is OrderState.OUTBOXED
    assert len(oms.pending_outbox()) == 2


def test_cancelled_order_requires_new_observation_before_reissue(tmp_path: Path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms, _ = build_service(tmp_path, ledger)
    first = arm_and_trigger(service)
    assert first.prepared is not None
    original_intent_id = first.prepared.record.intent_id
    acknowledge(oms, intent_id=original_intent_id)
    oms.transition(
        original_intent_id,
        OrderState.CANCEL_REQUESTED,
        event_id="cancel-requested:same-observation",
        occurred_at=NOW + timedelta(seconds=4),
    )
    oms.transition(
        original_intent_id,
        OrderState.CANCELLED,
        event_id="cancelled:same-observation",
        occurred_at=NOW + timedelta(seconds=5),
    )

    with pytest.raises(
        RuntimeError,
        match="PROTECTION_TERMINAL_ORDER_REQUIRES_NEW_OBSERVATION",
    ):
        service.observe_and_prepare(
            symbol="AAPL",
            reference_price=Decimal("101"),
            observed_at=NOW + timedelta(seconds=1),
            mark_prices={"AAPL": Decimal("101")},
        )


def test_existing_created_order_recovers_same_intent_to_outbox(tmp_path: Path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms, _ = build_service(tmp_path, ledger)
    service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
        mark_prices={"AAPL": Decimal("102")},
    )
    decision = service.protection.observe(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert decision.exit_target is not None
    intent = order_intent_for_target(
        decision.exit_target,
        current_quantity=Decimal("1"),
    )
    assert intent is not None
    client_order_id = service.lifecycle.client_order_id(intent)
    created = oms.create(
        intent,
        client_order_id=client_order_id,
        occurred_at=decision.exit_target.generated_at,
    )
    assert created.state is OrderState.CREATED

    recovered = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=2),
        mark_prices={"AAPL": Decimal("101")},
    )

    assert recovered.existing_order_reused is True
    assert recovered.prepared is not None
    assert recovered.prepared.record.intent_id == intent.intent_id
    assert recovered.prepared.record.state is OrderState.OUTBOXED
    assert len(oms.pending_outbox()) == 1


def test_filled_oms_with_stale_ledger_fails_closed_instead_of_second_sell(
    tmp_path: Path,
) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms, _ = build_service(tmp_path, ledger)
    first = arm_and_trigger(service)
    assert first.prepared is not None
    intent_id = first.prepared.record.intent_id
    acknowledge(oms, intent_id=intent_id)
    oms.apply_cumulative_fill(
        intent_id,
        event_id="filled-without-ledger",
        cumulative_filled=Decimal("1"),
        occurred_at=NOW + timedelta(seconds=4),
        broker_order_id="broker-protection-1",
    )
    assert oms.get(intent_id).state is OrderState.FILLED
    assert ledger.position("AAPL").quantity == Decimal("1")

    with pytest.raises(
        RuntimeError,
        match="PROTECTION_FILLED_ORDER_AWAITING_LEDGER_RECONCILIATION",
    ):
        service.observe_and_prepare(
            symbol="AAPL",
            reference_price=Decimal("98"),
            observed_at=NOW + timedelta(seconds=5),
            mark_prices={"AAPL": Decimal("98")},
        )
    assert len(oms.pending_outbox()) == 1
