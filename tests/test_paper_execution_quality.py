from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.paper_execution_quality import (
    PaperExecutionQualityTracker,
    SQLitePaperExecutionQualityStore,
)
from app.domain.trading import Fill, OrderIntent, Side
from app.oms.store import DurableOmsStore

NOW = datetime(2026, 8, 12, 3, 30, tzinfo=UTC)


class EffectiveLimit:
    def __init__(self, price: Decimal) -> None:
        self.price = price

    def current_limit_price(self, intent_id: str, *, fallback: Decimal) -> Decimal:
        del intent_id, fallback
        return self.price


def create_order(
    oms: DurableOmsStore,
    *,
    intent_id: str,
    side: Side,
    limit_price: str,
) -> OrderIntent:
    order = OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=side,
        quantity=Decimal("1"),
        limit_price=Decimal(limit_price),
        created_at=NOW,
        strategy_id="cross-sectional-quality-v2-paper-shadow",
    )
    oms.create(order, client_order_id=f"client-{intent_id}", occurred_at=NOW)
    return order


def fill(order: OrderIntent, *, fill_id: str, price: str) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id=order.intent_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=Decimal(price),
        occurred_at=NOW + timedelta(seconds=1),
    )


def test_buy_and_sell_slippage_use_positive_values_for_adverse_execution(
    tmp_path: Path,
) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = PaperExecutionQualityTracker(oms=oms, store=store)
    buy = create_order(oms, intent_id="buy", side=Side.BUY, limit_price="100")
    sell = create_order(oms, intent_id="sell", side=Side.SELL, limit_price="101")

    tracker.observe_fill(fill(buy, fill_id="buy-fill", price="100.20"))
    tracker.observe_fill(fill(sell, fill_id="sell-fill", price="100"))

    observations = store.fills()
    assert len(observations) == 2
    assert observations[0].signed_slippage_fraction == Decimal("0.002")
    assert observations[0].signed_slippage_notional == Decimal("0.20")
    assert observations[1].signed_slippage_fraction == Decimal("1") / Decimal("101")
    assert observations[1].signed_slippage_notional == Decimal("1")
    summary = store.summary()
    assert summary.fill_count == 2
    assert summary.adverse_fill_count == 2
    assert summary.favorable_fill_count == 0
    assert summary.signed_slippage_notional == Decimal("1.20")


def test_favorable_sell_execution_is_negative_signed_slippage(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = PaperExecutionQualityTracker(oms=oms, store=store)
    sell = create_order(oms, intent_id="sell", side=Side.SELL, limit_price="101")

    tracker.observe_fill(fill(sell, fill_id="sell-better", price="102"))

    observation = store.fills()[0]
    assert observation.signed_slippage_fraction == -Decimal("1") / Decimal("101")
    assert observation.signed_slippage_notional == Decimal("-1")
    summary = store.summary(side=Side.SELL)
    assert summary.adverse_fill_count == 0
    assert summary.favorable_fill_count == 1
    assert summary.worst_signed_slippage_bps is not None
    assert summary.worst_signed_slippage_bps < 0


def test_successful_replace_effective_limit_is_execution_baseline(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = PaperExecutionQualityTracker(
        oms=oms,
        store=store,
        effective_limits=EffectiveLimit(Decimal("99")),
    )
    sell = create_order(oms, intent_id="sell", side=Side.SELL, limit_price="101")

    tracker.observe_fill(fill(sell, fill_id="sell-replaced", price="98.80"))

    observation = store.fills()[0]
    assert observation.expected_limit_price == Decimal("99")
    assert observation.signed_slippage_fraction == Decimal("0.20") / Decimal("99")
    assert observation.signed_slippage_notional == Decimal("0.20")


def test_fill_replay_is_idempotent_and_conflicting_reuse_fails_closed(
    tmp_path: Path,
) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    tracker = PaperExecutionQualityTracker(oms=oms, store=store)
    buy = create_order(oms, intent_id="buy", side=Side.BUY, limit_price="100")
    exact = fill(buy, fill_id="same-fill", price="100.10")

    tracker.observe_fill(exact)
    tracker.observe_fill(exact)
    assert len(store.fills()) == 1

    with pytest.raises(ValueError, match="PAPER_EXECUTION_QUALITY_FILL_CONFLICT"):
        tracker.observe_fill(fill(buy, fill_id="same-fill", price="100.20"))
