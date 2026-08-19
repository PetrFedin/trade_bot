from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.paper_execution_quality import SQLitePaperExecutionQualityStore
from app.application.paper_execution_quality_v2 import (
    ReplayStablePaperExecutionQualityTracker,
)
from app.domain.trading import Fill, OrderIntent, Side
from app.oms.store import DurableOmsStore

NOW = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)


class TimelineLimitReference:
    def __init__(self, timeline: tuple[tuple[datetime, Decimal], ...]) -> None:
        self.timeline = timeline

    def limit_price_for_fill(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        fallback: Decimal,
    ) -> Decimal:
        del intent_id
        applicable = tuple(
            price for timestamp, price in self.timeline if timestamp <= occurred_at
        )
        return fallback if not applicable else applicable[-1]


def order(oms: DurableOmsStore) -> OrderIntent:
    result = OrderIntent(
        intent_id="protective-sell",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow:protection",
    )
    oms.create(result, client_order_id="protective-client", occurred_at=NOW)
    return result


def fill(*, fill_id: str, price: str, occurred_at: datetime) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id="protective-sell",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal(price),
        occurred_at=occurred_at,
    )


def test_replay_does_not_recompute_baseline_after_later_replacement(
    tmp_path: Path,
) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    order(oms)
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    timeline = TimelineLimitReference(
        (
            (NOW + timedelta(seconds=1), Decimal("99")),
            (NOW + timedelta(seconds=3), Decimal("98")),
        )
    )
    tracker = ReplayStablePaperExecutionQualityTracker(
        oms=oms,
        store=store,
        limit_reference=timeline,
    )
    partial = fill(
        fill_id="partial-at-99",
        price="98.80",
        occurred_at=NOW + timedelta(seconds=2),
    )

    tracker.observe_fill(partial)
    first = store.fills()[0]
    assert first.expected_limit_price == Decimal("99")
    assert first.signed_slippage_notional == Decimal("0.20")

    tracker.limit_reference = TimelineLimitReference(
        ((NOW + timedelta(seconds=1), Decimal("98")),)
    )
    tracker.observe_fill(partial)
    replayed = store.fills()[0]
    assert replayed == first
    assert replayed.expected_limit_price == Decimal("99")


def test_late_first_observation_uses_limit_at_fill_timestamp(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    order(oms)
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = ReplayStablePaperExecutionQualityTracker(
        oms=oms,
        store=store,
        limit_reference=TimelineLimitReference(
            (
                (NOW + timedelta(seconds=1), Decimal("99")),
                (NOW + timedelta(seconds=3), Decimal("98")),
            )
        ),
    )

    tracker.observe_fill(
        fill(
            fill_id="late-partial",
            price="98.90",
            occurred_at=NOW + timedelta(seconds=2),
        )
    )

    observation = store.fills()[0]
    assert observation.expected_limit_price == Decimal("99")
    assert observation.signed_slippage_notional == Decimal("0.10")


def test_default_reference_is_stable_original_order_limit(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    order(oms)
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = ReplayStablePaperExecutionQualityTracker(oms=oms, store=store)

    tracker.observe_fill(
        fill(
            fill_id="original-limit",
            price="100.50",
            occurred_at=NOW + timedelta(seconds=1),
        )
    )

    observation = store.fills()[0]
    assert observation.expected_limit_price == Decimal("101")
    assert observation.signed_slippage_notional == Decimal("0.50")


def test_reused_fill_id_with_different_economics_fails_closed(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    order(oms)
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = ReplayStablePaperExecutionQualityTracker(oms=oms, store=store)
    original = fill(
        fill_id="same-id",
        price="100",
        occurred_at=NOW + timedelta(seconds=1),
    )
    tracker.observe_fill(original)

    with pytest.raises(ValueError, match="PAPER_EXECUTION_QUALITY_FILL_CONFLICT"):
        tracker.observe_fill(
            fill(
                fill_id="same-id",
                price="99",
                occurred_at=NOW + timedelta(seconds=1),
            )
        )
