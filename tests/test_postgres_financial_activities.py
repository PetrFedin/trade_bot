from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip("PostgreSQL financial activity tests require ASTRA_TEST_POSTGRES_DSN", allow_module_level=True)

from app.execution.alpaca_financial_activities import FinancialActivityProjector
from app.execution.financial_activity_store import (
    BrokerFinancialActivity,
    FinancialProjectionState,
    PostgresFinancialActivityStore,
)
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.strict import StrictPostgresPortfolioEventStore

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
ACCOUNT = "paper-account:pg-fingerprint"
RELEASE = "release:f21b-pg"


def activity(activity_id: str, activity_type: str, amount: str) -> BrokerFinancialActivity:
    payload = {
        "id": activity_id,
        "activity_type": activity_type,
        "date": "2026-09-15",
        "net_amount": amount,
    }
    return BrokerFinancialActivity(
        activity_id=activity_id,
        activity_type=activity_type,
        net_amount=Decimal(amount),
        currency="USD",
        symbol=None,
        occurred_at=NOW,
        account_identity=ACCOUNT,
        release_identity=RELEASE,
        source_cursor="ROOT",
        canonical_payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def reset_tables() -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE astra_financial_activity_conflicts,
            astra_financial_activity_projection,
            astra_financial_activity_facts,
            astra_financial_activity_recovery,
            astra_portfolio_snapshots,
            astra_portfolio_events RESTART IDENTITY CASCADE"""
        )


def stack():
    portfolio = StrictPostgresPortfolioEventStore(DSN)
    portfolio.migrate()
    store = PostgresFinancialActivityStore(DSN)
    store.migrate()
    reset_tables()
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    projector = FinancialActivityProjector(
        store=store,
        portfolio=portfolio,
        runtime_ledger=ledger,
    )
    return store, portfolio, ledger, projector


def test_postgres_fee_and_deposit_projection_survive_restart() -> None:
    store, portfolio, ledger, projector = stack()
    store.ingest(activity("pg-deposit", "CSD", "100"), ingested_at=NOW)
    store.ingest(activity("pg-fee", "FEE", "-4"), ingested_at=NOW)
    projected, quarantined = projector.project_pending(occurred_at=NOW)
    assert projected == 2 and quarantined == 0
    assert ledger.cash == Decimal("1096")
    assert ledger.external_cash_flow == Decimal("100")
    assert ledger.fees_paid == Decimal("4")
    assert ledger.snapshot({}).total_pnl == Decimal("-4")

    restarted = portfolio.replay(opening_cash=Decimal("1000"))
    assert restarted.cash == Decimal("1096")
    assert restarted.external_cash_flow == Decimal("100")
    assert restarted.fees_paid == Decimal("4")
    assert restarted.snapshot({}).total_pnl == Decimal("-4")


def test_postgres_same_id_conflict_quarantines_and_cursor_is_monotonic() -> None:
    store, _, _, projector = stack()
    first = activity("pg-conflict", "CSD", "10")
    assert store.ingest(first, ingested_at=NOW).state is FinancialProjectionState.PENDING
    projector.project_pending(occurred_at=NOW)

    changed = activity("pg-conflict", "CSD", "11")
    record = store.ingest(changed, ingested_at=NOW)
    assert record.state is FinancialProjectionState.QUARANTINED
    assert record.reason == "ACTIVITY_ID_CONFLICT"
    assert store.quarantined_count() == 1

    state = store.advance_recovery(
        account_identity=ACCOUNT,
        release_identity=RELEASE,
        recovered_through=NOW,
        occurred_at=NOW,
    )
    assert state.recovered_through == NOW
    with pytest.raises(ValueError, match="WATERMARK_REGRESSION"):
        store.advance_recovery(
            account_identity=ACCOUNT,
            release_identity=RELEASE,
            recovered_through=NOW.replace(hour=11),
            occurred_at=NOW,
        )
