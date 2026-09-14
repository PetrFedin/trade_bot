from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.domain.trading import Bar
from app.risk.pretrade import OperationalRiskContext, RiskContext, RiskLimits

NOW = datetime(2026, 9, 13, 0, 30, tzinfo=UTC)


def config(
    *,
    position_fraction: Decimal = Decimal("0.20"),
    sector_fraction: Decimal = Decimal("0.40"),
) -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("10000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
            maximum_position_fraction_of_equity=position_fraction,
            maximum_sector_fraction_of_equity=sector_fraction,
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def measured_context(
    *,
    available_cash: Decimal = Decimal("10000"),
    adtv: Decimal = Decimal("1000000"),
    sector_notional: Decimal = Decimal("0"),
    volatility: Decimal = Decimal("0.20"),
) -> OperationalRiskContext:
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
        average_daily_dollar_volume=adtv,
        portfolio_equity=Decimal("10000"),
        sector_notional=sector_notional,
        annualized_volatility=volatility,
        available_cash=available_cash,
        portfolio_mark_prices={"AAPL": Decimal("102")},
    )


def test_f13_incomplete_generic_context_is_rejected_before_risk_evidence_or_oms(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    incomplete = RiskContext(
        price_timestamp=NOW,
        decision_time=NOW,
        available_cash=runtime.portfolio.cash,
    )

    with pytest.raises(ValueError, match="OPERATIONAL_RISK_CONTEXT_REQUIRED"):
        runtime.paper_pipeline.plan(
            bars(),
            decision_time=NOW,
            risk_context=incomplete,
        )

    assert runtime.risk_admission.journal.verify() == ()
    assert runtime.oms_store.pending_outbox() == ()


def test_complete_operational_context_allows_normal_admission_and_is_evidenced(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    _, intent, decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=measured_context(available_cash=runtime.portfolio.cash),
    )

    assert intent is not None
    assert decision is not None and decision.approved
    records = runtime.risk_admission.journal.verify()
    assert len(records) == 1
    context = records[0].payload["inputs"]["context"]
    assert context["average_daily_dollar_volume"] == "1000000"
    assert context["portfolio_equity"] == "10000"
    assert context["sector_notional"] == "0"
    assert context["annualized_volatility"] == "0.20"
    assert context["portfolio_mark_prices"] == {"AAPL": "102"}


def test_complete_operational_observations_activate_liquidity_control(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    _, intent, decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=measured_context(adtv=Decimal("500")),
    )
    assert intent is not None
    assert decision is not None and "LIQUIDITY_PARTICIPATION_EXCEEDED" in decision.reasons


def test_complete_operational_observations_activate_position_control(tmp_path) -> None:
    runtime = build_local_product(
        config=config(position_fraction=Decimal("0.005"), sector_fraction=Decimal("1")),
        state_directory=tmp_path,
    )
    _, intent, decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=measured_context(),
    )
    assert intent is not None
    assert decision is not None and "POSITION_CONCENTRATION_EXCEEDED" in decision.reasons


def test_complete_operational_observations_activate_sector_control(tmp_path) -> None:
    runtime = build_local_product(
        config=config(position_fraction=Decimal("1"), sector_fraction=Decimal("0.005")),
        state_directory=tmp_path,
    )
    _, intent, decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=measured_context(),
    )
    assert intent is not None
    assert decision is not None and "SECTOR_CONCENTRATION_EXCEEDED" in decision.reasons


def test_complete_operational_observations_activate_volatility_control(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    _, intent, decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=measured_context(volatility=Decimal("3")),
    )
    assert intent is not None
    assert decision is not None and "VOLATILITY_LIMIT_EXCEEDED" in decision.reasons
