from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.paper_order_limit_history import (
    OrderLimitEventKind,
    SQLiteConfirmedOrderLimitHistory,
)
from app.domain.trading import OrderIntent, Side

NOW = datetime(2026, 8, 12, 5, 30, tzinfo=UTC)


def order() -> OrderIntent:
    return OrderIntent(
        intent_id="protective-sell",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow:protection",
    )


def test_limit_history_resolves_effective_price_at_fill_time(tmp_path: Path) -> None:
    history = SQLiteConfirmedOrderLimitHistory(tmp_path / "limits.sqlite")
    history.record_initial(order())
    history.record_confirmed_replace(
        intent_id="protective-sell",
        mutation_id="replace-a",
        limit_price=Decimal("99"),
        confirmed_at=NOW + timedelta(seconds=2),
        broker_order_id="broker-a",
    )
    history.record_confirmed_replace(
        intent_id="protective-sell",
        mutation_id="replace-b",
        limit_price=Decimal("98"),
        confirmed_at=NOW + timedelta(seconds=4),
        broker_order_id="broker-b",
    )

    assert history.limit_price_for_fill(
        "protective-sell",
        occurred_at=NOW + timedelta(seconds=1),
        fallback=Decimal("777"),
    ) == Decimal("101")
    assert history.limit_price_for_fill(
        "protective-sell",
        occurred_at=NOW + timedelta(seconds=3),
        fallback=Decimal("777"),
    ) == Decimal("99")
    assert history.limit_price_for_fill(
        "protective-sell",
        occurred_at=NOW + timedelta(seconds=5),
        fallback=Decimal("777"),
    ) == Decimal("98")

    events = history.events("protective-sell")
    assert [event.event_kind for event in events] == [
        OrderLimitEventKind.INITIAL,
        OrderLimitEventKind.REPLACE_CONFIRMED,
        OrderLimitEventKind.REPLACE_CONFIRMED,
    ]
    assert [event.limit_price for event in events] == [
        Decimal("101"),
        Decimal("99"),
        Decimal("98"),
    ]


def test_requested_or_failed_replace_never_changes_history(tmp_path: Path) -> None:
    history = SQLiteConfirmedOrderLimitHistory(tmp_path / "limits.sqlite")
    history.record_initial(order())

    assert history.limit_price_for_fill(
        "protective-sell",
        occurred_at=NOW + timedelta(hours=1),
        fallback=Decimal("101"),
    ) == Decimal("101")
    assert len(history.events("protective-sell")) == 1


def test_confirmed_replace_replay_is_idempotent_and_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    history = SQLiteConfirmedOrderLimitHistory(tmp_path / "limits.sqlite")
    history.record_initial(order())
    first = history.record_confirmed_replace(
        intent_id="protective-sell",
        mutation_id="replace-a",
        limit_price=Decimal("99"),
        confirmed_at=NOW + timedelta(seconds=2),
        broker_order_id="broker-a",
    )
    replay = history.record_confirmed_replace(
        intent_id="protective-sell",
        mutation_id="replace-a",
        limit_price=Decimal("99"),
        confirmed_at=NOW + timedelta(seconds=2),
        broker_order_id="broker-a",
    )
    assert replay == first
    assert len(history.events("protective-sell")) == 2

    with pytest.raises(ValueError, match="CONFIRMED_ORDER_LIMIT_EVENT_CONFLICT"):
        history.record_confirmed_replace(
            intent_id="protective-sell",
            mutation_id="replace-a",
            limit_price=Decimal("98"),
            confirmed_at=NOW + timedelta(seconds=2),
            broker_order_id="broker-a",
        )


def test_unknown_intent_uses_stable_fallback(tmp_path: Path) -> None:
    history = SQLiteConfirmedOrderLimitHistory(tmp_path / "limits.sqlite")
    assert history.limit_price_for_fill(
        "missing-intent",
        occurred_at=NOW,
        fallback=Decimal("123.45"),
    ) == Decimal("123.45")
