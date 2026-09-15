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

NOW = datetime(2026, 9, 14, 18, 45, tzinfo=UTC)


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
    runtime = build_postgres_product(config=config(), dsn=DSN, migrate=True)
    mismatch = evidence(
        runtime,
        broker_cash=runtime.portfolio.cash - Decimal("10"),
        at=NOW,
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
        at=NOW + timedelta(seconds=1),
    )
    assert matched.matched
    assert restarted.portfolio_reconciliation.append(matched)
    assert restarted.portfolio_reconciliation.latest() == matched
