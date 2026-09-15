from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.trading import OrderIntent, Side
from app.risk.evidence import RiskAdmissionService, SQLiteRiskEvidenceJournal
from app.risk.pretrade import (
    OperationalRiskContext,
    PreTradeRiskEngine,
    RiskContext,
    RiskEvaluationMode,
    RiskLimits,
)

NOW = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_order_notional=Decimal("500"),
        maximum_symbol_notional=Decimal("1000"),
        maximum_gross_notional=Decimal("2000"),
        maximum_price_age_seconds=Decimal("10"),
        maximum_spread_bps=Decimal("20"),
        maximum_slippage_bps=Decimal("25"),
        maximum_daily_loss=Decimal("100"),
        maximum_drawdown=Decimal("150"),
        maximum_turnover_notional=Decimal("1000"),
        maximum_liquidity_participation_fraction=Decimal("0.10"),
        maximum_position_fraction_of_equity=Decimal("0.20"),
        maximum_sector_fraction_of_equity=Decimal("0.40"),
        maximum_annualized_volatility=Decimal("0.50"),
    )


def intent(intent_id: str = "f13-mode") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f13-qualification",
    )


def operational_context() -> OperationalRiskContext:
    return OperationalRiskContext(
        price_timestamp=NOW,
        decision_time=NOW,
        market_open=True,
        halted=False,
        spread_bps=Decimal("1"),
        estimated_slippage_bps=Decimal("1"),
        daily_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        turnover_notional=Decimal("0"),
        average_daily_dollar_volume=Decimal("1000000"),
        portfolio_equity=Decimal("1000"),
        sector_notional=Decimal("0"),
        annualized_volatility=Decimal("0.20"),
        available_cash=Decimal("1000"),
        portfolio_mark_prices={"AAPL": Decimal("100")},
    )


def test_direct_operational_evaluation_rejects_missing_context() -> None:
    engine = PreTradeRiskEngine(limits())
    with pytest.raises(ValueError, match="OPERATIONAL_RISK_CONTEXT_REQUIRED"):
        engine.evaluate(
            intent(),
            mode=RiskEvaluationMode.OPERATIONAL,
            current_symbol_notional=Decimal("0"),
            current_gross_notional=Decimal("0"),
            context=None,
        )


def test_direct_operational_evaluation_rejects_generic_optimistic_context() -> None:
    engine = PreTradeRiskEngine(limits())
    generic = RiskContext(
        price_timestamp=NOW,
        decision_time=NOW,
        available_cash=Decimal("1000"),
    )
    with pytest.raises(ValueError, match="OPERATIONAL_RISK_CONTEXT_REQUIRED"):
        engine.evaluate(
            intent(),
            mode=RiskEvaluationMode.OPERATIONAL,
            current_symbol_notional=Decimal("0"),
            current_gross_notional=Decimal("0"),
            context=generic,
        )


def test_direct_operational_evaluation_accepts_complete_measured_context() -> None:
    decision = PreTradeRiskEngine(limits()).evaluate(
        intent(),
        mode=RiskEvaluationMode.OPERATIONAL,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=operational_context(),
    )
    assert decision.approved
    assert decision.reasons == ()


def test_replay_without_operational_observations_is_explicit_not_implicit() -> None:
    decision = PreTradeRiskEngine(limits()).evaluate(
        intent(),
        mode=RiskEvaluationMode.REPLAY,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=None,
    )
    assert decision.approved
    assert decision.reasons == ()


def test_operational_admission_rejects_generic_context_before_evidence(tmp_path) -> None:
    journal = SQLiteRiskEvidenceJournal(tmp_path / "risk.sqlite")
    service = RiskAdmissionService(
        engine=PreTradeRiskEngine(limits()),
        journal=journal,
    )
    generic = RiskContext(
        price_timestamp=NOW,
        decision_time=NOW,
        available_cash=Decimal("1000"),
    )
    with pytest.raises(ValueError, match="OPERATIONAL_RISK_CONTEXT_REQUIRED"):
        service.evaluate_and_record(
            intent("f13-admission-reject"),
            mode=RiskEvaluationMode.OPERATIONAL,
            current_symbol_notional=Decimal("0"),
            current_gross_notional=Decimal("0"),
            context=generic,
            evaluated_at=NOW,
        )
    assert journal.verify() == ()


def test_operational_admission_evidence_commits_mode_and_full_context(tmp_path) -> None:
    journal = SQLiteRiskEvidenceJournal(tmp_path / "risk.sqlite")
    service = RiskAdmissionService(
        engine=PreTradeRiskEngine(limits()),
        journal=journal,
    )
    recorded = service.evaluate_and_record(
        intent("f13-admission-pass"),
        mode=RiskEvaluationMode.OPERATIONAL,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=operational_context(),
        evaluated_at=NOW,
    )
    assert recorded.decision.approved
    evidence = journal.verify()
    assert len(evidence) == 1
    assert evidence[0].payload["inputs"]["mode"] == "OPERATIONAL"
    context = evidence[0].payload["inputs"]["context"]
    assert context["market_open"] is True
    assert context["halted"] is False
    assert context["spread_bps"] == "1"
    assert context["average_daily_dollar_volume"] == "1000000"
    assert context["portfolio_equity"] == "1000"
    assert context["annualized_volatility"] == "0.20"
