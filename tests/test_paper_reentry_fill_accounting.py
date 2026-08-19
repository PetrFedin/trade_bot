from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.paper_reentry import (
    PaperReentryController,
    SQLitePaperReentryStore,
)
from app.domain.trading import Fill, OrderIntent, Side
from app.execution.trade_fills import (
    ExactBrokerFill,
    ExplicitZeroPaperFeeModel,
    PaperTradeFillAccounting,
)
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.store import PortfolioEventStore
from app.strategy.cross_sectional_selection import CrossSectionalSelection
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy

NOW = datetime(2026, 8, 11, 22, 30, tzinfo=UTC)


def controller(tmp_path: Path) -> PaperReentryController:
    return PaperReentryController(
        store=SQLitePaperReentryStore(tmp_path / "paper-reentry.sqlite"),
        policy=ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2),
    )


def seed_long(
    portfolio: PortfolioEventStore,
    ledger: PortfolioLedger,
) -> None:
    fill = Fill(
        fill_id="seed-aapl",
        order_intent_id="seed-intent-aapl",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW - timedelta(minutes=1),
    )
    assert portfolio.append_fill(fill)
    ledger.apply_fill(fill)


def prepare_acknowledged(
    oms: DurableOmsStore,
    *,
    intent_id: str,
    client_order_id: str,
    broker_order_id: str,
    side: Side,
    quantity: str = "1",
    price: str = "101",
) -> OrderIntent:
    order = OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=side,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow",
    )
    oms.create(order, client_order_id=client_order_id, occurred_at=NOW)
    oms.approve_risk(order.intent_id, event_id=f"risk:{intent_id}", occurred_at=NOW)
    oms.enqueue_submit(
        order.intent_id,
        event_id=f"outbox:{intent_id}",
        occurred_at=NOW,
    )
    oms.transition(
        order.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id=f"submit:{intent_id}",
        occurred_at=NOW,
    )
    oms.transition(
        order.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id=f"ack:{intent_id}",
        occurred_at=NOW,
        broker_order_id=broker_order_id,
    )
    return order


def exact_fill(
    *,
    execution_id: str,
    broker_order_id: str,
    client_order_id: str,
    side: Side,
    price: str,
    occurred_at: datetime,
) -> ExactBrokerFill:
    return ExactBrokerFill(
        execution_id=execution_id,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        symbol="AAPL",
        side=side,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal(price),
        occurred_at=occurred_at,
    )


class FailOnceObserver:
    def __init__(self, delegate: PaperReentryController) -> None:
        self.delegate = delegate
        self.failed = False

    def observe_fill(self, fill: Fill) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated observer crash")
        self.delegate.observe_fill(fill)


def selected_aapl(decision_time: datetime) -> CrossSectionalSelection:
    return CrossSectionalSelection(
        decision_time=decision_time,
        selected_symbols=("AAPL",),
        candidates=(),
    )


def test_fill_replay_repairs_reentry_after_portfolio_commit_crash_window(
    tmp_path: Path,
) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(portfolio, ledger)
    gate = controller(tmp_path)
    observer = FailOnceObserver(gate)
    order = prepare_acknowledged(
        oms,
        intent_id="exit-intent",
        client_order_id="exit-client",
        broker_order_id="exit-broker",
        side=Side.SELL,
    )
    broker_fill = exact_fill(
        execution_id="exit-exec",
        broker_order_id="exit-broker",
        client_order_id="exit-client",
        side=Side.SELL,
        price="101",
        occurred_at=NOW + timedelta(seconds=1),
    )
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=ledger,
        fill_observer=observer,
    )

    with pytest.raises(RuntimeError, match="simulated observer crash"):
        accounting.apply(order.intent_id, broker_fill)

    assert ledger.position("AAPL").quantity == 0
    assert portfolio.replay(opening_cash=Decimal("1000")).position("AAPL").quantity == 0
    assert oms.get(order.intent_id).state is OrderState.ACKNOWLEDGED
    assert oms.get(order.intent_id).filled_quantity == 0
    assert gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL") is None

    repaired = accounting.apply(order.intent_id, broker_fill)
    assert repaired.portfolio_event_appended is False
    assert repaired.oms_advanced is True
    assert repaired.record.state is OrderState.FILLED
    armed = gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL")
    assert armed is not None
    assert armed.consecutive_selected_decisions == 0

    first_signal_time = NOW + timedelta(minutes=1)
    first_signal = gate.evaluate_selection(selected_aapl(first_signal_time))
    assert first_signal[0].confirmation_streak == 1
    assert first_signal[0].allow_entry is False

    duplicate = accounting.apply(order.intent_id, broker_fill)
    assert duplicate.portfolio_event_appended is False
    assert duplicate.oms_advanced is False
    after_duplicate = gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL")
    assert after_duplicate is not None
    assert after_duplicate.consecutive_selected_decisions == 1


def test_exact_buy_fill_clears_armed_reentry_state(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(portfolio, ledger)
    gate = controller(tmp_path)
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=ledger,
        fill_observer=gate,
    )

    exit_order = prepare_acknowledged(
        oms,
        intent_id="exit-intent",
        client_order_id="exit-client",
        broker_order_id="exit-broker",
        side=Side.SELL,
    )
    accounting.apply(
        exit_order.intent_id,
        exact_fill(
            execution_id="exit-exec",
            broker_order_id="exit-broker",
            client_order_id="exit-client",
            side=Side.SELL,
            price="101",
            occurred_at=NOW + timedelta(seconds=1),
        ),
    )
    assert gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL") is not None

    buy_order = prepare_acknowledged(
        oms,
        intent_id="entry-intent",
        client_order_id="entry-client",
        broker_order_id="entry-broker",
        side=Side.BUY,
        price="102",
    )
    accounting.apply(
        buy_order.intent_id,
        exact_fill(
            execution_id="entry-exec",
            broker_order_id="entry-broker",
            client_order_id="entry-client",
            side=Side.BUY,
            price="102",
            occurred_at=NOW + timedelta(seconds=2),
        ),
    )

    assert ledger.position("AAPL").quantity == Decimal("1")
    assert gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL") is None
