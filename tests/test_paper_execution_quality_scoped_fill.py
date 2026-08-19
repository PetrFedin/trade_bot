from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_execution_quality import (
    PaperExecutionQualityTracker,
    SQLitePaperExecutionQualityStore,
)
from app.application.paper_strategy_scope import (
    SQLitePaperStrategyIntentRegistry,
    StrategyScopedPaperFillObserver,
)
from app.domain.trading import OrderIntent, Side
from app.execution.trade_fills import (
    ExactBrokerFill,
    ExplicitZeroPaperFeeModel,
    PaperTradeFillAccounting,
)
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.store import PortfolioEventStore

NOW = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def prepare(
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


def exact_fill(
    order: OrderIntent,
    *,
    execution_id: str,
    client_order_id: str,
    broker_order_id: str,
    price: str,
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
        occurred_at=NOW + timedelta(seconds=1),
    )


def test_global_fill_accounting_tracks_execution_only_for_scoped_strategy(
    tmp_path: Path,
) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    execution_store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    execution = PaperExecutionQualityTracker(oms=oms, store=execution_store)
    scoped = StrategyScopedPaperFillObserver(
        strategy_id=STRATEGY,
        registry=registry,
        observer=execution,
    )
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=ledger,
        fill_observer=scoped,
    )

    owned = OrderIntent(
        intent_id="owned-buy",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id=STRATEGY,
    )
    prepare(
        oms,
        owned,
        client_order_id="owned-client",
        broker_order_id="owned-broker",
    )
    registry.register(owned)
    accounting.apply(
        owned.intent_id,
        exact_fill(
            owned,
            execution_id="owned-exec",
            client_order_id="owned-client",
            broker_order_id="owned-broker",
            price="100.20",
        ),
    )

    foreign = OrderIntent(
        intent_id="foreign-buy",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("50"),
        created_at=NOW + timedelta(seconds=2),
        strategy_id="other-strategy",
    )
    prepare(
        oms,
        foreign,
        client_order_id="foreign-client",
        broker_order_id="foreign-broker",
    )
    registry.register(foreign)
    accounting.apply(
        foreign.intent_id,
        exact_fill(
            foreign,
            execution_id="foreign-exec",
            client_order_id="foreign-client",
            broker_order_id="foreign-broker",
            price="50.50",
        ),
    )

    observations = execution_store.fills()
    assert len(observations) == 1
    assert observations[0].symbol == "AAPL"
    assert observations[0].signed_slippage_notional == Decimal("0.20")
    assert ledger.position("AAPL").quantity == Decimal("1")
    assert ledger.position("MSFT").quantity == Decimal("1")
