from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_intent_evidence import SQLitePaperIntentEvidenceRegistry
from app.domain.trading import OrderIntent, Side

NOW = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="protective-intent",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id=f"{STRATEGY}:protection",
    )


def test_combined_registry_records_scope_and_initial_limit_idempotently(
    tmp_path: Path,
) -> None:
    registry = SQLitePaperIntentEvidenceRegistry(tmp_path / "evidence.sqlite")
    order = intent()
    effective_at = NOW + timedelta(seconds=1)

    first = registry.register(
        order,
        strategy_id=STRATEGY,
        registered_at=effective_at,
    )
    replay = registry.register(
        order,
        strategy_id=STRATEGY,
        registered_at=effective_at,
    )

    assert replay == first
    assert registry.get(order.intent_id) == first
    events = registry.limit_history.events(order.intent_id)
    assert len(events) == 1
    assert events[0].limit_price == Decimal("101")
    assert events[0].effective_at == effective_at
    assert registry.limit_history.limit_price_for_fill(
        order.intent_id,
        occurred_at=effective_at + timedelta(seconds=1),
        fallback=Decimal("999"),
    ) == Decimal("101")
