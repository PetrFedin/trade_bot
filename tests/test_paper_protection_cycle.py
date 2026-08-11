from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.paper_protection import (
    PaperProtectionService,
    PaperProtectionStatus,
    SQLitePaperProtectionStore,
)
from app.application.paper_protection_cycle import PaperProtectionOrderService
from app.application.portfolio_paper_planner import (
    PortfolioPaperDisposition,
    PortfolioPaperPlanner,
)
from app.domain.trading import Fill, Side
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.position_management import ExitReason, PositionManagementPolicy

NOW = datetime(2026, 8, 11, 21, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Gate:
    allow_new_entries: bool
    allow_exits: bool = True
    reasons: tuple[str, ...] = ()


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


def build_service(tmp_path, ledger: PortfolioLedger):
    oms = DurableOmsStore(tmp_path / "protection-oms.sqlite")
    protection = PaperProtectionService(
        ledger=ledger,
        store=SQLitePaperProtectionStore(tmp_path / "protection-state.sqlite"),
        policy=policy(),
    )
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())
    lifecycle = PaperOrderLifecycle(oms)
    service = PaperProtectionOrderService(
        protection=protection,
        planner=planner,
        lifecycle=lifecycle,
    )
    return service, oms


def arm_profit_peak(service: PaperProtectionOrderService) -> None:
    result = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
        mark_prices={"AAPL": Decimal("102")},
    )
    assert result.protection.status is PaperProtectionStatus.TRACKING
    assert result.prepared is None


def test_fresh_price_protection_trigger_reaches_durable_outbox(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms = build_service(tmp_path, ledger)
    arm_profit_peak(service)

    triggered = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
        quality_gate=Gate(
            allow_new_entries=False,
            reasons=("CONSECUTIVE_LOSS_LIMIT_REACHED",),
        ),
    )

    assert triggered.protection.status is PaperProtectionStatus.EXIT_PENDING
    assert triggered.protection.exit_reason is ExitReason.PROFIT_PROTECTION
    assert triggered.plan_item is not None
    assert triggered.plan_item.disposition is PortfolioPaperDisposition.RISK_APPROVED
    assert triggered.plan_item.intent is not None
    assert triggered.plan_item.intent.side is Side.SELL
    assert triggered.prepared is not None
    assert triggered.prepared.record.state is OrderState.OUTBOXED
    assert triggered.prepared.record.limit_price == Decimal("101")
    assert triggered.existing_order_reused is False
    assert len(oms.pending_outbox()) == 1


def test_retry_after_outbox_reuses_original_order_without_repricing(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms = build_service(tmp_path, ledger)
    arm_profit_peak(service)
    first = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
    )
    assert first.prepared is not None

    retry = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=2),
        mark_prices={"AAPL": Decimal("99")},
    )

    assert retry.existing_order_reused is True
    assert retry.prepared is not None
    assert retry.prepared.record.intent_id == first.prepared.record.intent_id
    assert retry.prepared.client_order_id == first.prepared.client_order_id
    assert retry.prepared.record.limit_price == Decimal("101")
    assert len(oms.pending_outbox()) == 1


def test_partial_fill_after_outbox_cannot_create_second_protective_sell(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms = build_service(tmp_path, ledger)
    arm_profit_peak(service)
    first = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
    )
    assert first.prepared is not None

    ledger.apply_fill(
        Fill(
            fill_id="partial-protection-fill",
            order_intent_id=first.prepared.record.intent_id,
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("0.4"),
            price=Decimal("100.5"),
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    assert ledger.position("AAPL").quantity == Decimal("0.6")

    retry = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=3),
        mark_prices={"AAPL": Decimal("99")},
    )

    assert retry.existing_order_reused is True
    assert retry.prepared is not None
    assert retry.prepared.record.intent_id == first.prepared.record.intent_id
    assert len(oms.pending_outbox()) == 1


def test_risk_rejected_exit_reprices_before_first_durable_outbox(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms = build_service(tmp_path, ledger)
    arm_profit_peak(service)

    rejected = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
        kill_switch_engaged=True,
    )
    assert rejected.plan_item is not None
    assert rejected.plan_item.disposition is PortfolioPaperDisposition.RISK_REJECTED
    assert rejected.plan_item.reasons == ("KILL_SWITCH_ENGAGED",)
    assert rejected.prepared is None
    assert oms.pending_outbox() == ()

    approved = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=2),
        mark_prices={"AAPL": Decimal("99")},
        kill_switch_engaged=False,
    )
    assert approved.plan_item is not None and approved.plan_item.approved
    assert approved.protection.exit_target is not None
    assert approved.protection.exit_target.reference_price == Decimal("99")
    assert approved.prepared is not None
    assert approved.prepared.record.limit_price == Decimal("99")
    assert len(oms.pending_outbox()) == 1


def test_quantity_change_before_first_outbox_fails_closed(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    service, oms = build_service(tmp_path, ledger)
    arm_profit_peak(service)

    rejected = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
        kill_switch_engaged=True,
    )
    assert rejected.prepared is None
    assert oms.pending_outbox() == ()

    ledger.apply_fill(
        Fill(
            fill_id="external-partial-exit",
            order_intent_id="external-partial-exit-intent",
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("0.1"),
            price=Decimal("100"),
            occurred_at=NOW + timedelta(seconds=2),
        )
    )

    with pytest.raises(
        RuntimeError,
        match="PROTECTION_POSITION_CHANGED_BEFORE_DURABLE_OUTBOX",
    ):
        service.observe_and_prepare(
            symbol="AAPL",
            reference_price=Decimal("99"),
            observed_at=NOW + timedelta(seconds=3),
            mark_prices={"AAPL": Decimal("99")},
        )
    assert oms.pending_outbox() == ()
