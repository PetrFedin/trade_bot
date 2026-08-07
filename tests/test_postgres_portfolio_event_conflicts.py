from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL portfolio conflict tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import Fill, Side
from app.portfolio.strict import StrictPostgresPortfolioEventStore

NOW = datetime(2026, 8, 7, 19, 15, tzinfo=UTC)


def fill(*, price: str = "100", quantity: str = "1", occurred_at: datetime = NOW) -> Fill:
    return Fill(
        fill_id="broker:pg-exec-conflict",
        order_intent_id="pg-intent-conflict",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        occurred_at=occurred_at,
    )


@pytest.fixture()
def store() -> StrictPostgresPortfolioEventStore:
    value = StrictPostgresPortfolioEventStore(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("TRUNCATE astra_portfolio_snapshots, astra_portfolio_events RESTART IDENTITY")
    return value


def test_concurrent_identical_execution_is_recorded_once(
    store: StrictPostgresPortfolioEventStore,
) -> None:
    exact = fill()

    def append(_: int) -> bool:
        return StrictPostgresPortfolioEventStore(DSN).append_fill(exact)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(append, range(4)))
    assert results.count(True) == 1
    assert results.count(False) == 3
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
def test_same_execution_id_with_different_economics_is_rejected(
    store: StrictPostgresPortfolioEventStore,
    conflicting: Fill,
) -> None:
    assert store.append_fill(fill()) is True
    with pytest.raises(ValueError, match="PORTFOLIO_EVENT_CONFLICT"):
        store.append_fill(conflicting)
    ledger = store.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("1")
    assert ledger.position("AAPL").average_cost == Decimal("100")
