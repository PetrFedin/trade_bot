from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.trading import Fill, Side
from app.portfolio.strict import StrictPortfolioEventStore

NOW = datetime(2026, 8, 7, 19, 0, tzinfo=UTC)


def fill(*, price: str = "100", quantity: str = "1", occurred_at: datetime = NOW) -> Fill:
    return Fill(
        fill_id="broker:exec-conflict",
        order_intent_id="intent-conflict",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        occurred_at=occurred_at,
    )


def test_identical_fill_replay_is_idempotent(tmp_path) -> None:
    store = StrictPortfolioEventStore(tmp_path / "portfolio.sqlite")
    exact = fill()
    assert store.append_fill(exact) is True
    assert store.append_fill(exact) is False
    ledger = store.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("1")
    assert ledger.cash == Decimal("900")


@pytest.mark.parametrize(
    "conflicting",
    [
        fill(price="101"),
        fill(quantity="0.5"),
        fill(occurred_at=NOW + timedelta(seconds=1)),
    ],
)
def test_same_execution_id_with_different_economics_is_rejected(tmp_path, conflicting: Fill) -> None:
    store = StrictPortfolioEventStore(tmp_path / "portfolio.sqlite")
    assert store.append_fill(fill()) is True
    with pytest.raises(ValueError, match="PORTFOLIO_EVENT_CONFLICT"):
        store.append_fill(conflicting)
    ledger = store.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("1")
    assert ledger.position("AAPL").average_cost == Decimal("100")
