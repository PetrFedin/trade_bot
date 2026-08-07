from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL portfolio tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import Fill, Side
from app.portfolio.postgres import PostgresPortfolioEventStore

NOW = datetime(2026, 8, 7, 19, 0, tzinfo=UTC)


def fill(fill_id: str = "pg-portfolio-fill") -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id="pg-portfolio-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        occurred_at=NOW,
        fee=Decimal("1"),
    )


@pytest.fixture()
def store() -> PostgresPortfolioEventStore:
    value = PostgresPortfolioEventStore(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_portfolio_snapshots, astra_portfolio_events RESTART IDENTITY"
        )
    return value


def test_postgres_portfolio_replay_and_snapshot_survive_restart(
    store: PostgresPortfolioEventStore,
) -> None:
    assert store.append_fill(fill())
    assert not store.append_fill(fill())
    assert store.append_split(
        action_id="pg-split-1",
        symbol="AAPL",
        ratio=Decimal("2"),
        occurred_at=NOW,
    )
    assert store.append_cash_dividend(
        action_id="pg-dividend-1",
        symbol="AAPL",
        amount_per_share=Decimal("1.5"),
        occurred_at=NOW,
    )

    reopened = PostgresPortfolioEventStore(DSN)
    ledger = reopened.replay(opening_cash=Decimal("10000"))
    position = ledger.position("AAPL")
    assert position.quantity == Decimal("20")
    assert position.average_cost == Decimal("50.05")
    assert ledger.cash == Decimal("9029.0")
    assert ledger.cash_income == Decimal("30.0")

    persisted = reopened.persist_snapshot(
        ledger,
        prices={"AAPL": Decimal("50")},
        occurred_at=NOW,
    )
    latest = PostgresPortfolioEventStore(DSN).latest_snapshot()
    assert latest is not None
    assert latest.snapshot_id == persisted.snapshot_id
    assert latest.payload == persisted.payload
    assert latest.payload["equity"] == "10029.0"
    assert latest.payload["total_pnl"] == "29.0"


def test_concurrent_duplicate_portfolio_event_is_recorded_once(
    store: PostgresPortfolioEventStore,
) -> None:
    def append_once(_: int) -> bool:
        return PostgresPortfolioEventStore(DSN).append_fill(fill("shared-fill"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        inserted = list(executor.map(append_once, range(4)))
    assert inserted.count(True) == 1
    assert inserted.count(False) == 3
    with psycopg.connect(DSN) as connection:
        count = connection.execute(
            "SELECT count(*) FROM astra_portfolio_events WHERE event_id='fill:shared-fill'"
        ).fetchone()[0]
    assert count == 1


def test_postgres_portfolio_events_and_snapshots_are_database_append_only(
    store: PostgresPortfolioEventStore,
) -> None:
    store.append_fill(fill())
    ledger = store.replay(opening_cash=Decimal("10000"))
    store.persist_snapshot(ledger, prices={"AAPL": Decimal("100")}, occurred_at=NOW)

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_portfolio_events SET event_type='SPLIT' WHERE sequence=1"
            )
        connection.rollback()
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_portfolio_snapshots WHERE snapshot_id=1"
            )
        connection.rollback()
