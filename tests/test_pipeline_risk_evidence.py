from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.paper_pipeline import PaperTradingPipeline
from app.domain.trading import Bar
from app.portfolio.ledger import PortfolioLedger
from app.risk.evidence import RiskAdmissionService, SQLiteRiskEvidenceJournal
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.momentum import LongOnlyMomentumStrategy

NOW = datetime(2026, 8, 7, 19, 30, tzinfo=UTC)


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def build_pipeline(path):
    risk = PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        )
    )
    journal = SQLiteRiskEvidenceJournal(path)
    admission = RiskAdmissionService(engine=risk, journal=journal)
    return (
        PaperTradingPipeline(
            strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1")),
            ledger=PortfolioLedger(opening_cash=Decimal("10000")),
            risk=risk,
            risk_admission=admission,
        ),
        journal,
    )


def test_pipeline_persists_approved_risk_before_order_execution(tmp_path) -> None:
    pipeline, journal = build_pipeline(tmp_path / "pipeline-risk.sqlite")
    _, intent, decision = pipeline.plan(bars())
    assert intent is not None and decision is not None and decision.approved
    assert pipeline.last_recorded_risk is not None
    records = journal.verify()
    assert len(records) == 1
    assert records[0].intent_id == intent.intent_id
    assert records[0].payload["decision"]["approved"] is True
    assert pipeline.last_recorded_risk.evidence_digest == records[0].digest


def test_pipeline_persists_kill_switch_rejection(tmp_path) -> None:
    pipeline, journal = build_pipeline(tmp_path / "pipeline-risk-reject.sqlite")
    _, intent, decision = pipeline.plan(bars(), kill_switch_engaged=True)
    assert intent is not None and decision is not None and not decision.approved
    assert "KILL_SWITCH_ENGAGED" in decision.reasons
    records = journal.verify()
    assert len(records) == 1
    assert records[0].payload["decision"]["approved"] is False
