from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_execution_quality import SQLitePaperExecutionQualityStore
from app.application.paper_execution_quality_v2 import (
    ReplayStablePaperExecutionQualityTracker,
)
from app.application.paper_order_limit_history import SQLiteConfirmedOrderLimitHistory
from app.application.paper_reprice_evidence import PaperConfirmedReplacementRecorder
from app.domain.trading import Fill, OrderIntent, Side
from app.oms.store import DurableOmsStore

NOW = datetime(2026, 8, 12, 6, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Mutation:
    mutation_id: str
    intent_id: str
    target_limit_price: Decimal | None


def protective_order(oms: DurableOmsStore) -> OrderIntent:
    order = OrderIntent(
        intent_id="protective-sell",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow:protection",
    )
    oms.create(order, client_order_id="protective-client", occurred_at=NOW)
    return order


def test_confirmed_replace_becomes_historical_fill_baseline(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    order = protective_order(oms)
    history = SQLiteConfirmedOrderLimitHistory(tmp_path / "limits.sqlite")
    history.record_initial(order)
    recorder = PaperConfirmedReplacementRecorder(history)
    mutation = Mutation(
        mutation_id="replace-a",
        intent_id=order.intent_id,
        target_limit_price=Decimal("99"),
    )
    recorder.record(
        mutation,
        confirmed_at=NOW + timedelta(seconds=2),
        broker_order_id="broker-replaced-a",
    )
    execution_store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = ReplayStablePaperExecutionQualityTracker(
        oms=oms,
        store=execution_store,
        limit_reference=history,
    )

    tracker.observe_fill(
        Fill(
            fill_id="fill-after-replace",
            order_intent_id=order.intent_id,
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("1"),
            price=Decimal("98.80"),
            occurred_at=NOW + timedelta(seconds=3),
        )
    )

    observation = execution_store.fills()[0]
    assert observation.expected_limit_price == Decimal("99")
    assert observation.signed_slippage_notional == Decimal("0.20")


def test_fill_before_confirmed_replace_uses_initial_limit(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    order = protective_order(oms)
    history = SQLiteConfirmedOrderLimitHistory(tmp_path / "limits.sqlite")
    history.record_initial(order)
    recorder = PaperConfirmedReplacementRecorder(history)
    recorder.record(
        Mutation(
            mutation_id="replace-a",
            intent_id=order.intent_id,
            target_limit_price=Decimal("99"),
        ),
        confirmed_at=NOW + timedelta(seconds=4),
    )
    execution_store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = ReplayStablePaperExecutionQualityTracker(
        oms=oms,
        store=execution_store,
        limit_reference=history,
    )

    tracker.observe_fill(
        Fill(
            fill_id="fill-before-replace-confirmed",
            order_intent_id=order.intent_id,
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("1"),
            price=Decimal("100.50"),
            occurred_at=NOW + timedelta(seconds=3),
        )
    )

    observation = execution_store.fills()[0]
    assert observation.expected_limit_price == Decimal("101")
    assert observation.signed_slippage_notional == Decimal("0.50")
