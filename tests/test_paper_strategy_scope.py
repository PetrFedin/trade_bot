from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.paper_strategy_scope import (
    SQLitePaperStrategyIntentRegistry,
    StrategyScopedPaperFillObserver,
)
from app.domain.trading import Fill, OrderIntent, Side

NOW = datetime(2026, 8, 12, 1, 30, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


class RecordingObserver:
    def __init__(self) -> None:
        self.fill_ids: list[str] = []

    def observe_fill(self, fill: Fill) -> None:
        self.fill_ids.append(fill.fill_id)


def intent(
    intent_id: str,
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    strategy_id: str = STRATEGY,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=side,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id=strategy_id,
    )


def fill_for(
    order: OrderIntent,
    *,
    fill_id: str,
    symbol: str | None = None,
    side: Side | None = None,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id=order.intent_id,
        symbol=order.symbol if symbol is None else symbol,
        side=order.side if side is None else side,
        quantity=order.quantity,
        price=order.limit_price,
        occurred_at=NOW + timedelta(seconds=1),
    )


def test_registry_is_idempotent_and_rejects_ownership_conflict(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "strategy-scope.sqlite")
    order = intent("intent-1")

    first = registry.register(order)
    replay = registry.register(order)
    assert replay == first
    assert registry.get(order.intent_id) == first

    with pytest.raises(ValueError, match="PAPER_STRATEGY_INTENT_CONFLICT"):
        registry.register(order, strategy_id="another-strategy")


def test_protection_suffix_can_be_scoped_to_base_strategy(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "strategy-scope.sqlite")
    protection_order = intent(
        "protective-intent",
        side=Side.SELL,
        strategy_id=f"{STRATEGY}:protection",
    )

    ownership = registry.register(
        protection_order,
        strategy_id=STRATEGY,
    )
    assert ownership.strategy_id == STRATEGY
    assert ownership.side is Side.SELL
    assert registry.get(protection_order.intent_id) == ownership


def test_scoped_observer_ignores_missing_and_other_strategy_fills(tmp_path: Path) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "strategy-scope.sqlite")
    delegate = RecordingObserver()
    scoped = StrategyScopedPaperFillObserver(
        strategy_id=STRATEGY,
        registry=registry,
        observer=delegate,
    )
    unregistered = intent("unregistered")
    scoped.observe_fill(fill_for(unregistered, fill_id="fill-unregistered"))

    foreign = intent("foreign", strategy_id="other-strategy")
    registry.register(foreign)
    scoped.observe_fill(fill_for(foreign, fill_id="fill-foreign"))
    assert delegate.fill_ids == []


def test_scoped_observer_routes_matching_fill_and_fails_closed_on_identity_drift(
    tmp_path: Path,
) -> None:
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "strategy-scope.sqlite")
    delegate = RecordingObserver()
    scoped = StrategyScopedPaperFillObserver(
        strategy_id=STRATEGY,
        registry=registry,
        observer=delegate,
    )
    order = intent("owned-intent")
    registry.register(order)

    scoped.observe_fill(fill_for(order, fill_id="fill-owned"))
    assert delegate.fill_ids == ["fill-owned"]

    with pytest.raises(ValueError, match="PAPER_STRATEGY_FILL_SYMBOL_MISMATCH"):
        scoped.observe_fill(
            fill_for(order, fill_id="fill-wrong-symbol", symbol="MSFT")
        )
    with pytest.raises(ValueError, match="PAPER_STRATEGY_FILL_SIDE_MISMATCH"):
        scoped.observe_fill(
            fill_for(order, fill_id="fill-wrong-side", side=Side.SELL)
        )
