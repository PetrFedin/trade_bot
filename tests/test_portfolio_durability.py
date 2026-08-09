from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.domain.trading import Fill, Side
from app.portfolio.store import PortfolioEventStore

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 16, 0, tzinfo=UTC)


def buy_fill() -> Fill:
    return Fill(
        fill_id="portfolio-fill-1",
        order_intent_id="intent-portfolio-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        occurred_at=NOW,
        fee=Decimal("0"),
    )


def test_portfolio_event_replay_survives_restart(tmp_path) -> None:
    path = tmp_path / "portfolio.sqlite"
    store = PortfolioEventStore(path)
    assert store.append_fill(buy_fill())
    assert not store.append_fill(buy_fill())
    assert store.append_split(
        action_id="split-1",
        symbol="AAPL",
        ratio=Decimal("2"),
        occurred_at=NOW,
    )
    assert not store.append_split(
        action_id="split-1",
        symbol="AAPL",
        ratio=Decimal("2"),
        occurred_at=NOW,
    )
    assert store.append_cash_dividend(
        action_id="dividend-1",
        symbol="AAPL",
        amount_per_share=Decimal("1.5"),
        occurred_at=NOW,
    )

    reopened = PortfolioEventStore(path)
    ledger = reopened.replay(opening_cash=Decimal("10000"))
    position = ledger.position("AAPL")
    assert position.quantity == Decimal("20")
    assert position.average_cost == Decimal("50")
    assert ledger.cash == Decimal("9030.0")
    assert ledger.cash_income == Decimal("30.0")

    snapshot = reopened.persist_snapshot(
        ledger,
        prices={"AAPL": Decimal("50")},
        occurred_at=NOW,
    )
    assert snapshot.payload["equity"] == "10030.0"
    assert snapshot.payload["total_pnl"] == "30.0"
    assert snapshot.payload["cash_income"] == "30.0"

    latest = PortfolioEventStore(path).latest_snapshot()
    assert latest is not None
    assert latest.snapshot_id == snapshot.snapshot_id
    assert latest.payload == snapshot.payload


def test_split_preserves_position_cost_basis_value(tmp_path) -> None:
    store = PortfolioEventStore(tmp_path / "split.sqlite")
    store.append_fill(buy_fill())
    before = store.replay(opening_cash=Decimal("10000")).position("AAPL")
    store.append_split(
        action_id="split-2",
        symbol="AAPL",
        ratio=Decimal("4"),
        occurred_at=NOW,
    )
    after = store.replay(opening_cash=Decimal("10000")).position("AAPL")
    assert before.quantity * before.average_cost == after.quantity * after.average_cost
    assert after.quantity == Decimal("40")
    assert after.average_cost == Decimal("25")


def test_dividend_for_unowned_symbol_records_zero_income(tmp_path) -> None:
    store = PortfolioEventStore(tmp_path / "dividend.sqlite")
    store.append_cash_dividend(
        action_id="dividend-no-position",
        symbol="MSFT",
        amount_per_share=Decimal("2"),
        occurred_at=NOW,
    )
    ledger = store.replay(opening_cash=Decimal("10000"))
    assert ledger.cash == Decimal("10000")
    assert ledger.cash_income == Decimal("0")
