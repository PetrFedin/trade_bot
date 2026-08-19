from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.paper_reaction_quality import (
    SQLitePaperReactionQualityStore,
    StrategyPaperReactionTracker,
)
from app.application.paper_strategy_scope import SQLitePaperStrategyIntentRegistry
from app.domain.trading import Fill, OrderIntent, Side

NOW = datetime(2026, 8, 12, 7, 30, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def register(
    registry: SQLitePaperStrategyIntentRegistry,
    *,
    intent_id: str,
    symbol: str,
    side: Side,
    strategy_id: str = STRATEGY,
    registered_at: datetime = NOW,
) -> OrderIntent:
    order = OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=side,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=registered_at,
        strategy_id=strategy_id,
    )
    registry.register(order, strategy_id=strategy_id, registered_at=registered_at)
    return order


def fill(
    order: OrderIntent,
    *,
    fill_id: str,
    occurred_at: datetime,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id=order.intent_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=order.limit_price,
        occurred_at=occurred_at,
    )


def test_reaction_tracker_measures_entry_and_exit_latency(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    store = SQLitePaperReactionQualityStore(tmp_path / "reaction.sqlite")
    tracker = StrategyPaperReactionTracker(
        strategy_id=STRATEGY,
        registry=registry,
        store=store,
    )
    entry = register(
        registry,
        intent_id="entry-aapl",
        symbol="AAPL",
        side=Side.BUY,
    )
    exit_order = register(
        registry,
        intent_id="exit-aapl",
        symbol="AAPL",
        side=Side.SELL,
        registered_at=NOW + timedelta(seconds=10),
    )

    tracker.observe_fill(
        fill(entry, fill_id="entry-fill", occurred_at=NOW + timedelta(seconds=4))
    )
    tracker.observe_fill(
        fill(
            exit_order,
            fill_id="exit-fill",
            occurred_at=NOW + timedelta(seconds=13),
        )
    )

    all_summary = store.summary(strategy_id=STRATEGY)
    assert all_summary.fill_count == 2
    assert all_summary.average_latency_seconds == Decimal("3.5")
    assert all_summary.maximum_latency_seconds == Decimal("4.0")
    entry_summary = store.summary(strategy_id=STRATEGY, side=Side.BUY)
    assert entry_summary.average_latency_seconds == Decimal("4.0")
    exit_summary = store.summary(strategy_id=STRATEGY, side=Side.SELL)
    assert exit_summary.average_latency_seconds == Decimal("3.0")


def test_reaction_tracker_ignores_foreign_strategy_fill(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    store = SQLitePaperReactionQualityStore(tmp_path / "reaction.sqlite")
    tracker = StrategyPaperReactionTracker(
        strategy_id=STRATEGY,
        registry=registry,
        store=store,
    )
    foreign = register(
        registry,
        intent_id="foreign-entry",
        symbol="MSFT",
        side=Side.BUY,
        strategy_id="other-strategy",
    )

    tracker.observe_fill(
        fill(foreign, fill_id="foreign-fill", occurred_at=NOW + timedelta(seconds=1))
    )

    assert store.summary(strategy_id=STRATEGY).fill_count == 0


def test_reaction_fill_replay_is_idempotent(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    store = SQLitePaperReactionQualityStore(tmp_path / "reaction.sqlite")
    tracker = StrategyPaperReactionTracker(
        strategy_id=STRATEGY,
        registry=registry,
        store=store,
    )
    entry = register(
        registry,
        intent_id="entry-aapl",
        symbol="AAPL",
        side=Side.BUY,
    )
    exact = fill(entry, fill_id="same-fill", occurred_at=NOW + timedelta(seconds=2))

    tracker.observe_fill(exact)
    tracker.observe_fill(exact)
    assert store.summary(strategy_id=STRATEGY).fill_count == 1


def test_fill_before_registered_decision_fails_closed(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    store = SQLitePaperReactionQualityStore(tmp_path / "reaction.sqlite")
    tracker = StrategyPaperReactionTracker(
        strategy_id=STRATEGY,
        registry=registry,
        store=store,
    )
    entry = register(
        registry,
        intent_id="entry-aapl",
        symbol="AAPL",
        side=Side.BUY,
        registered_at=NOW + timedelta(seconds=5),
    )

    with pytest.raises(ValueError, match="PAPER_REACTION_FILL_PRECEDES_DECISION"):
        tracker.observe_fill(
            fill(entry, fill_id="early-fill", occurred_at=NOW + timedelta(seconds=4))
        )
