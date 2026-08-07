from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.trading import OrderIntent, Side
from app.risk.evidence import RiskAdmissionService, SQLiteRiskEvidenceJournal
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskLimits

NOW = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_order_notional=Decimal("2000"),
        maximum_symbol_notional=Decimal("5000"),
        maximum_gross_notional=Decimal("10000"),
        maximum_daily_loss=Decimal("500"),
        maximum_drawdown=Decimal("750"),
    )


def intent(intent_id: str = "risk-evidence-1", quantity: str = "10") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(quantity),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="risk-evidence-validation",
    )


def context() -> RiskContext:
    return RiskContext(
        price_timestamp=NOW - timedelta(seconds=1),
        decision_time=NOW,
        market_open=True,
        spread_bps=Decimal("5"),
        estimated_slippage_bps=Decimal("5"),
        portfolio_equity=Decimal("10000"),
        average_daily_dollar_volume=Decimal("100000"),
        annualized_volatility=Decimal("0.25"),
    )


def service(path) -> RiskAdmissionService:
    return RiskAdmissionService(
        engine=PreTradeRiskEngine(limits()),
        journal=SQLiteRiskEvidenceJournal(path),
    )


def test_approved_and_rejected_decisions_are_immutably_recorded(tmp_path) -> None:
    path = tmp_path / "risk.sqlite"
    admission = service(path)
    approved = admission.evaluate_and_record(
        intent(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    assert approved.decision.approved
    assert len(approved.decision_id) == 64
    assert len(approved.evidence_digest) == 64

    rejected = admission.evaluate_and_record(
        intent("risk-evidence-2", quantity="30"),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    assert not rejected.decision.approved
    assert "ORDER_NOTIONAL_LIMIT_EXCEEDED" in rejected.decision.reasons

    records = SQLiteRiskEvidenceJournal(path).verify()
    assert [record.sequence for record in records] == [1, 2]
    assert records[1].previous_digest == records[0].digest
    assert records[0].payload["decision"]["approved"] is True
    assert records[1].payload["decision"]["approved"] is False


def test_same_intent_and_same_evidence_is_idempotent(tmp_path) -> None:
    path = tmp_path / "risk-idempotent.sqlite"
    admission = service(path)
    first = admission.evaluate_and_record(
        intent(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    second = admission.evaluate_and_record(
        intent(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    assert second == first
    assert len(SQLiteRiskEvidenceJournal(path).verify()) == 1


def test_same_intent_with_changed_inputs_is_a_conflict(tmp_path) -> None:
    path = tmp_path / "risk-conflict.sqlite"
    admission = service(path)
    admission.evaluate_and_record(
        intent(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    with pytest.raises(ValueError, match="RISK_DECISION_CONFLICT"):
        admission.evaluate_and_record(
            intent(),
            current_symbol_notional=Decimal("100"),
            current_gross_notional=Decimal("100"),
            context=context(),
            evaluated_at=NOW,
        )


def test_tampered_risk_payload_fails_chain_verification(tmp_path) -> None:
    path = tmp_path / "risk-tamper.sqlite"
    admission = service(path)
    admission.evaluate_and_record(
        intent(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=context(),
        evaluated_at=NOW,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE risk_decisions SET payload=? WHERE intent_id=?",
            ('{"tampered":true}', "risk-evidence-1"),
        )
    with pytest.raises(RuntimeError, match="RISK_EVIDENCE_DIGEST_MISMATCH"):
        SQLiteRiskEvidenceJournal(path).verify()
