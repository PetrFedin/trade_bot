from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.domain.trading import Bar, Fill, Side
from app.risk.pretrade import OperationalRiskContext, RiskLimits

NOW = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("10000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
            maximum_position_fraction_of_equity=Decimal("1"),
            maximum_sector_fraction_of_equity=Decimal("1"),
        ),
    )


def msft_bars() -> list[Bar]:
    return [
        Bar("MSFT", NOW - timedelta(minutes=2), Decimal("200")),
        Bar("MSFT", NOW - timedelta(minutes=1), Decimal("201")),
        Bar("MSFT", NOW, Decimal("202")),
    ]


def context(runtime, *, marks: dict[str, Decimal], equity: Decimal) -> OperationalRiskContext:
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
        portfolio_equity=equity,
        sector_notional=Decimal("0"),
        annualized_volatility=Decimal("0.20"),
        available_cash=runtime.portfolio.cash,
        portfolio_mark_prices=marks,
    )


def runtime_with_aapl_position(tmp_path):
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    runtime.portfolio.apply_fill(
        Fill(
            fill_id="f08-aapl-fill",
            order_intent_id="f08-aapl-intent",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=NOW - timedelta(minutes=3),
        )
    )
    assert runtime.portfolio.cash == Decimal("9900")
    return runtime


def test_f08_missing_mark_for_existing_position_fails_before_risk_evidence(tmp_path) -> None:
    runtime = runtime_with_aapl_position(tmp_path)

    with pytest.raises(ValueError, match="PORTFOLIO_VALUATION_INCOMPLETE:AAPL"):
        runtime.paper_pipeline.plan(
            msft_bars(),
            decision_time=NOW,
            risk_context=context(
                runtime,
                marks={"MSFT": Decimal("202")},
                equity=Decimal("10000"),
            ),
        )

    assert runtime.risk_admission.journal.verify() == ()
    assert runtime.oms_store.pending_outbox() == ()


def test_f08_complete_marks_value_existing_position_and_new_target(tmp_path) -> None:
    runtime = runtime_with_aapl_position(tmp_path)
    marks = {"AAPL": Decimal("100"), "MSFT": Decimal("202")}

    _, intent, decision = runtime.paper_pipeline.plan(
        msft_bars(),
        decision_time=NOW,
        risk_context=context(runtime, marks=marks, equity=Decimal("10000")),
    )

    assert intent is not None and intent.symbol == "MSFT"
    assert decision is not None and decision.approved
    assert decision.order_notional == Decimal("202")
    assert decision.projected_symbol_notional == Decimal("202")
    assert decision.projected_gross_notional == Decimal("302")
    records = runtime.risk_admission.journal.verify()
    assert len(records) == 1
    evidence_context = records[0].payload["inputs"]["context"]
    assert evidence_context["portfolio_mark_prices"] == {
        "AAPL": "100",
        "MSFT": "202",
    }


def test_f08_target_mark_must_equal_strategy_reference_price(tmp_path) -> None:
    runtime = runtime_with_aapl_position(tmp_path)

    with pytest.raises(ValueError, match="TARGET_MARK_PRICE_MISMATCH"):
        runtime.paper_pipeline.plan(
            msft_bars(),
            decision_time=NOW,
            risk_context=context(
                runtime,
                marks={"AAPL": Decimal("100"), "MSFT": Decimal("201")},
                equity=Decimal("10000"),
            ),
        )

    assert runtime.risk_admission.journal.verify() == ()


def test_f08_supplied_equity_must_match_durable_marked_portfolio(tmp_path) -> None:
    runtime = runtime_with_aapl_position(tmp_path)

    with pytest.raises(ValueError, match="PORTFOLIO_EQUITY_MISMATCH"):
        runtime.paper_pipeline.plan(
            msft_bars(),
            decision_time=NOW,
            risk_context=context(
                runtime,
                marks={"AAPL": Decimal("100"), "MSFT": Decimal("202")},
                equity=Decimal("9999"),
            ),
        )

    assert runtime.risk_admission.journal.verify() == ()
