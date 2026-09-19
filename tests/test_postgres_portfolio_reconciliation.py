from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL reconciliation tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.composition import ProductConfig, build_postgres_product
from app.oms.portfolio_reconciliation import build_portfolio_reconciliation_evidence
from app.oms.reconciliation import BrokerPortfolioTruth, BrokerPositionTruth, reconcile_portfolio
from app.risk.pretrade import RiskLimits


def fresh_start() -> datetime:
    """Return an instant later than every reconciliation already stored.

    reconciliation_id is derived solely from occurred_at, and
    astra_portfolio_reconciliations is append-only behind a BEFORE TRUNCATE guard
    (ASTRA_PORTFOLIO_RECONCILIATION_TRUNCATE_FORBIDDEN), so rows written by earlier
    runs cannot be cleared. Against a persistent database a fixed instant collides on
    re-run, and wall-clock alone is not enough because this test also writes a row one
    second ahead of its start. Starting past the newest stored row keeps each run
    isolated, and keeps latest() pointing at this run, without weakening append-only.
    """
    with psycopg.connect(DSN, autocommit=True) as connection:
        row = connection.execute(
            "SELECT max(occurred_at) FROM astra_portfolio_reconciliations"
        ).fetchone()
    newest = row[0] if row else None
    now = datetime.now(UTC)
    if newest is None:
        return now
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    return max(now, newest.astimezone(UTC) + timedelta(seconds=5))


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("1000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        ),
    )


def broker_positions(runtime) -> tuple[BrokerPositionTruth, ...]:
    return tuple(
        BrokerPositionTruth(position.symbol, position.quantity)
        for position in runtime.portfolio.positions()
    )


def evidence(runtime, *, broker_cash: Decimal, at: datetime):
    truth = BrokerPortfolioTruth(cash=broker_cash, positions=broker_positions(runtime))
    result = reconcile_portfolio(runtime.portfolio, truth)
    return build_portfolio_reconciliation_evidence(
        runtime.portfolio,
        truth,
        result,
        occurred_at=at,
    )


def test_postgres_reconciliation_is_restart_safe_idempotent_and_conflict_aware() -> None:
    # One database is one account. Other modules in this suite open the same database at
    # a different opening cash, and the genesis binding refuses a reopen once the journal
    # has history, so the journal is cleared before this module claims the account. The
    # clear runs before any build, because the build is what the binding refuses.
    with psycopg.connect(DSN, autocommit=True) as connection:
        try:
            connection.execute("TRUNCATE astra_portfolio_events RESTART IDENTITY CASCADE")
        except psycopg.errors.UndefinedTable:
            pass  # first module to touch a fresh database; migrate creates it below
    runtime = build_postgres_product(config=config(), dsn=DSN, migrate=True)
    now = fresh_start()
    mismatch = evidence(
        runtime,
        broker_cash=runtime.portfolio.cash - Decimal("10"),
        at=now,
    )
    assert mismatch.reasons == ("CASH_MISMATCH",)
    assert runtime.portfolio_reconciliation.append(mismatch)
    assert runtime.portfolio_reconciliation.append(mismatch) is False

    restarted = build_postgres_product(config=config(), dsn=DSN)
    assert restarted.portfolio_reconciliation.latest() == mismatch

    changed = replace(
        mismatch,
        broker_cash=mismatch.broker_cash - Decimal("1"),
        cash_delta=mismatch.cash_delta - Decimal("1"),
    )
    changed.validate()
    with pytest.raises(ValueError, match="PORTFOLIO_RECONCILIATION_CONFLICT"):
        restarted.portfolio_reconciliation.append(changed)

    matched = evidence(
        restarted,
        broker_cash=restarted.portfolio.cash,
        at=now + timedelta(seconds=1),
    )
    assert matched.matched
    assert restarted.portfolio_reconciliation.append(matched)
    assert restarted.portfolio_reconciliation.latest() == matched
