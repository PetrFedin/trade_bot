from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_reentry import (
    PaperReentryController,
    SQLitePaperReentryStore,
)
from app.application.paper_strategy_scope import (
    SQLitePaperStrategyIntentRegistry,
    StrategyScopedPaperFillObserver,
)
from app.application.paper_trade_quality import (
    PaperTradeQualityTracker,
    SQLitePaperTradeQualityStore,
)
from app.domain.trading import OrderIntent, Side
from app.execution.trade_fills import (
    CompositePaperFillObserver,
    ExactBrokerFill,
    ExplicitZeroPaperFeeModel,
    PaperTradeFillAccounting,
)
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.store import PortfolioEventStore
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy

NOW = datetime(2026, 8, 12, 2, 30, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def acknowledge(
    oms: DurableOmsStore,
    order: OrderIntent,
    *,
    client_order_id: str,
    broker_order_id: str,
) -> None:
    oms.create(order, client_order_id=client_order_id, occurred_at=order.created_at)
    oms.approve_risk(
        order.intent_id,
        event_id=f"risk:{order.intent_id}",
        occurred_at=order.created_at,
    )
    oms.enqueue_submit(
        order.intent_id,
        event_id=f"outbox:{order.intent_id}",
        occurred_at=order.created_at,
    )
    oms.transition(
        order.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id=f"submit:{order.intent_id}",
        occurred_at=order.created_at,
    )
    oms.transition(
        order.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id=f"ack:{order.intent_id}",
        occurred_at=order.created_at,
        broker_order_id=broker_order_id,
    )


def broker_fill(
    order: OrderIntent,
    *,
    execution_id: str,
    broker_order_id: str,
    client_order_id: str,
    price: str,
    occurred_at: datetime,
) -> ExactBrokerFill:
    return ExactBrokerFill(
        execution_id=execution_id,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        symbol=order.symbol,
        side=order.side,
        order_quantity=order.quantity,
        cumulative_quantity=order.quantity,
        quantity=order.quantity,
        price=Decimal(price),
        occurred_at=occurred_at,
    )


def order(
    intent_id: str,
    *,
    symbol: str,
    side: Side,
    strategy_id: str,
    price: str,
    created_at: datetime,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=side,
        quantity=Decimal("1"),
        limit_price=Decimal(price),
        created_at=created_at,
        strategy_id=strategy_id,
    )


def test_exact_fill_accounting_routes_only_owned_strategy_fills(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    reentry = PaperReentryController(
        store=SQLitePaperReentryStore(tmp_path / "reentry.sqlite"),
        policy=ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2),
        strategy_id=STRATEGY,
    )
    quality = PaperTradeQualityTracker(
        store=SQLitePaperTradeQualityStore(tmp_path / "quality.sqlite"),
        strategy_id=STRATEGY,
    )
    scoped = StrategyScopedPaperFillObserver(
        strategy_id=STRATEGY,
        registry=registry,
        observer=CompositePaperFillObserver(reentry, quality),
    )
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=ledger,
        fill_observer=scoped,
    )

    entry = order(
        "owned-entry",
        symbol="AAPL",
        side=Side.BUY,
        strategy_id=STRATEGY,
        price="100",
        created_at=NOW,
    )
    acknowledge(
        oms,
        entry,
        client_order_id="owned-entry-client",
        broker_order_id="owned-entry-broker",
    )
    registry.register(entry)
    accounting.apply(
        entry.intent_id,
        broker_fill(
            entry,
            execution_id="owned-entry-exec",
            broker_order_id="owned-entry-broker",
            client_order_id="owned-entry-client",
            price="100",
            occurred_at=NOW + timedelta(seconds=1),
        ),
    )
    owned_open = quality.store.open_trade(strategy_id=STRATEGY, symbol="AAPL")
    assert owned_open is not None
    assert owned_open.open_quantity == Decimal("1")

    foreign = order(
        "foreign-entry",
        symbol="MSFT",
        side=Side.BUY,
        strategy_id="other-strategy",
        price="50",
        created_at=NOW + timedelta(seconds=2),
    )
    acknowledge(
        oms,
        foreign,
        client_order_id="foreign-client",
        broker_order_id="foreign-broker",
    )
    registry.register(foreign)
    accounting.apply(
        foreign.intent_id,
        broker_fill(
            foreign,
            execution_id="foreign-exec",
            broker_order_id="foreign-broker",
            client_order_id="foreign-client",
            price="50",
            occurred_at=NOW + timedelta(seconds=3),
        ),
    )
    assert quality.store.open_trade(strategy_id=STRATEGY, symbol="MSFT") is None
    assert reentry.store.state(strategy_id=STRATEGY, symbol="MSFT") is None
    assert ledger.position("MSFT").quantity == Decimal("1")

    quality.observe_price(
        symbol="AAPL",
        reference_price=Decimal("105"),
        observed_at=NOW + timedelta(seconds=4),
    )
    exit_order = order(
        "owned-exit",
        symbol="AAPL",
        side=Side.SELL,
        strategy_id=f"{STRATEGY}:protection",
        price="104",
        created_at=NOW + timedelta(seconds=5),
    )
    acknowledge(
        oms,
        exit_order,
        client_order_id="owned-exit-client",
        broker_order_id="owned-exit-broker",
    )
    registry.register(exit_order, strategy_id=STRATEGY)
    quality.register_exit_intent(
        intent_id=exit_order.intent_id,
        symbol="AAPL",
        exit_reason="PROFIT_PROTECTION",
        registered_at=exit_order.created_at,
    )
    accounting.apply(
        exit_order.intent_id,
        broker_fill(
            exit_order,
            execution_id="owned-exit-exec",
            broker_order_id="owned-exit-broker",
            client_order_id="owned-exit-client",
            price="104",
            occurred_at=NOW + timedelta(seconds=6),
        ),
    )

    closed = quality.store.closed_trades(strategy_id=STRATEGY)
    assert len(closed) == 1
    assert closed[0].symbol == "AAPL"
    assert closed[0].exit_reason == "PROFIT_PROTECTION"
    assert closed[0].net_pnl == Decimal("4")
    armed = reentry.store.state(strategy_id=STRATEGY, symbol="AAPL")
    assert armed is not None
    assert armed.consecutive_selected_decisions == 0
    assert ledger.position("AAPL").quantity == 0
