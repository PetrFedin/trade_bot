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
        "PostgreSQL risk integration tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import OrderIntent, Side
from app.risk.evidence import RiskAdmissionService
from app.risk.postgres import PostgresRiskEvidenceJournal
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskLimits

NOW = datetime(2026, 8, 7, 18, 30, tzinfo=UTC)


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_order_notional=Decimal("2000"),
        maximum_symbol_notional=Decimal("5000"),
        maximum_gross_notional=Decimal("10000"),
    )


def intent(value: int) -> OrderIntent:
    return OrderIntent(
        intent_id=f"pg-risk-{value}",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="pg-risk-validation",
    )


def context() -> RiskContext:
    return RiskContext(price_timestamp=NOW, decision_time=NOW)


@pytest.fixture()
def journal() -> PostgresRiskEvidenceJournal:
    value = PostgresRiskEvidenceJournal(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("TRUNCATE astra_risk_decisions")
        connection.execute(
            """UPDATE astra_risk_chain_state
            SET last_sequence=0, last_digest=repeat('0', 64) WHERE singleton=TRUE"""
        )
    return value


def record(value: int) -> str:
    local_journal = PostgresRiskEvidenceJournal(DSN)
    result = RiskAdmissionService(
        engine=PreTradeRiskEngine(limits()),
        journal=local_journal,
    ).evaluate_and_record(
        intent(value),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    return result.evidence_digest


def test_postgres_risk_chain_serializes_concurrent_workers(
    journal: PostgresRiskEvidenceJournal,
) -> None:
    with ThreadPoolExecutor(max_workers=4) as executor:
        digests = list(executor.map(record, range(1, 5)))
    assert len(set(digests)) == 4
    records = journal.verify()
    assert len(records) == 4
    assert [item.sequence for item in records] == [1, 2, 3, 4]
    for previous, current in zip(records[:-1], records[1:], strict=True):
        assert current.previous_digest == previous.digest


def test_postgres_risk_decisions_are_database_append_only(
    journal: PostgresRiskEvidenceJournal,
) -> None:
    record(1)
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_risk_decisions SET intent_id='tampered' WHERE sequence=1"
            )
        connection.rollback()
    assert journal.verify()[0].intent_id == "pg-risk-1"


def test_postgres_same_intent_conflict_is_rejected(
    journal: PostgresRiskEvidenceJournal,
) -> None:
    service = RiskAdmissionService(
        engine=PreTradeRiskEngine(limits()),
        journal=journal,
    )
    service.evaluate_and_record(
        intent(1),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    with pytest.raises(ValueError, match="RISK_DECISION_CONFLICT"):
        service.evaluate_and_record(
            intent(1),
            current_symbol_notional=Decimal("100"),
            current_gross_notional=Decimal("100"),
            context=context(),
            evaluated_at=NOW,
        )
