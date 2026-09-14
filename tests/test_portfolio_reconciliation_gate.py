from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.domain.trading import Bar, Fill, Side
from app.observability.readiness import OperationalSnapshot
from app.oms.portfolio_reconciliation import build_portfolio_reconciliation_evidence
from app.oms.reconciliation import BrokerPortfolioTruth, BrokerPositionTruth, reconcile_portfolio
from app.oms.store import OrderRecord, OrderState
from app.risk.pretrade import OperationalRiskContext, RiskLimits
from app.runtime.paper_dispatch_control import DispatchBlocked, DispatchControlMode
from app.runtime.paper_final_dispatch import PaperFinalDispatchGuard

NOW = datetime(2026, 9, 14, 18, 30, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("1000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
            maximum_position_fraction_of_equity=Decimal("1"),
            maximum_sector_fraction_of_equity=Decimal("1"),
        ),
    )


def rising_bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def falling_bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("105")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("104")),
        Bar("AAPL", NOW, Decimal("100")),
    ]


def risk_context(runtime, *, price: str) -> OperationalRiskContext:
    marks = {"AAPL": Decimal(price)}
    position = runtime.portfolio.position("AAPL")
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
        portfolio_equity=runtime.portfolio.equity(marks),
        sector_notional=position.quantity * marks["AAPL"],
        annualized_volatility=Decimal("0.20"),
        available_cash=runtime.portfolio.cash,
        portfolio_mark_prices=marks,
    )


def ready_snapshot() -> OperationalSnapshot:
    return OperationalSnapshot(
        market_data_age_seconds=Decimal("0"),
        stream_silence_seconds=Decimal("0"),
        broker_latency_ms=Decimal("1"),
        broker_error_fraction=Decimal("0"),
        uncertain_orders=0,
        reconciliation_age_seconds=Decimal("0"),
        cash_mismatch=Decimal("0"),
        position_mismatches=0,
        daily_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        kill_switch_engaged=False,
        market_data_ready=True,
        stream_ready=True,
        broker_connected=True,
        portfolio_reconciled=True,
    )


def record_reconciliation(runtime, broker: BrokerPortfolioTruth, *, at: datetime):
    result = reconcile_portfolio(runtime.portfolio, broker)
    evidence = build_portfolio_reconciliation_evidence(
        runtime.portfolio,
        broker,
        result,
        occurred_at=at,
    )
    appended = runtime.portfolio_reconciliation.append(evidence)
    return result, evidence, appended


def order_record(*, side: Side) -> OrderRecord:
    return OrderRecord(
        intent_id=f"dispatch-{side.value.lower()}",
        client_order_id=f"client-{side.value.lower()}",
        broker_order_id="",
        symbol="AAPL",
        side=side,
        quantity=Decimal("1"),
        limit_price=Decimal("102"),
        filled_quantity=Decimal("0"),
        state=OrderState.OUTBOXED,
        version=3,
        updated_at=NOW,
    )


def test_known_cash_mismatch_blocks_buy_before_risk_and_survives_restart(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    result, evidence, appended = record_reconciliation(
        runtime,
        BrokerPortfolioTruth(cash=Decimal("100"), positions=()),
        at=NOW - timedelta(seconds=2),
    )
    assert appended
    assert not result.matched and result.reasons == ("CASH_MISMATCH",)
    assert runtime.portfolio_reconciliation.latest() == evidence

    with pytest.raises(RuntimeError, match="BROKER_PORTFOLIO_NOT_RECONCILED:CASH_MISMATCH"):
        runtime.paper_pipeline.plan(
            rising_bars(),
            decision_time=NOW,
            risk_context=risk_context(runtime, price="102"),
        )
    assert runtime.risk_admission.journal.verify() == ()
    assert runtime.oms_store.pending_outbox() == ()

    restarted = build_local_product(config=config(), state_directory=tmp_path)
    assert restarted.portfolio_reconciliation.latest() == evidence
    with pytest.raises(RuntimeError, match="BROKER_PORTFOLIO_NOT_RECONCILED:CASH_MISMATCH"):
        restarted.paper_pipeline.plan(
            rising_bars(),
            decision_time=NOW,
            risk_context=risk_context(restarted, price="102"),
        )
    assert restarted.risk_admission.journal.verify() == ()

    matched, _, _ = record_reconciliation(
        restarted,
        BrokerPortfolioTruth(cash=Decimal("1000"), positions=()),
        at=NOW - timedelta(seconds=1),
    )
    assert matched.matched
    _, intent, decision = restarted.paper_pipeline.plan(
        rising_bars(),
        decision_time=NOW,
        risk_context=risk_context(restarted, price="102"),
    )
    assert intent is not None and intent.side is Side.BUY
    assert decision is not None and decision.approved


def test_reconciliation_store_is_idempotent_and_conflict_aware(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    _, evidence, appended = record_reconciliation(
        runtime,
        BrokerPortfolioTruth(cash=Decimal("100"), positions=()),
        at=NOW,
    )
    assert appended
    assert runtime.portfolio_reconciliation.append(evidence) is False

    changed = replace(
        evidence,
        broker_cash=Decimal("99"),
        cash_delta=Decimal("-901"),
    )
    changed.validate()
    with pytest.raises(ValueError, match="PORTFOLIO_RECONCILIATION_CONFLICT"):
        runtime.portfolio_reconciliation.append(changed)


def test_known_mismatch_blocks_buy_dispatch_but_preserves_sell_authority(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    record_reconciliation(
        runtime,
        BrokerPortfolioTruth(cash=Decimal("100"), positions=()),
        at=NOW - timedelta(seconds=1),
    )
    runtime.dispatch_control.arm(
        operator_id="f21a-test",
        reason="bounded dispatch qualification",
        occurred_at=NOW,
    )
    guard = PaperFinalDispatchGuard(
        control=runtime.dispatch_control,
        readiness=runtime.operational_readiness,
        snapshot_provider=ready_snapshot,
        portfolio_reconciliation=runtime.portfolio_reconciliation,
    )

    with pytest.raises(DispatchBlocked) as blocked:
        guard.authorize(order_record(side=Side.BUY), occurred_at=NOW)
    assert blocked.value.reasons == ("BROKER_PORTFOLIO_NOT_RECONCILED", "CASH_MISMATCH")
    assert runtime.dispatch_control.current().mode is DispatchControlMode.ARMED

    sell_authorization = guard.authorize(order_record(side=Side.SELL), occurred_at=NOW)
    assert sell_authorization.intent_id == "dispatch-sell"

    restarted = build_local_product(config=config(), state_directory=tmp_path)
    restarted_guard = PaperFinalDispatchGuard(
        control=restarted.dispatch_control,
        readiness=restarted.operational_readiness,
        snapshot_provider=ready_snapshot,
        portfolio_reconciliation=restarted.portfolio_reconciliation,
    )
    with pytest.raises(DispatchBlocked) as blocked_after_restart:
        restarted_guard.authorize(order_record(side=Side.BUY), occurred_at=NOW)
    assert blocked_after_restart.value.reasons == (
        "BROKER_PORTFOLIO_NOT_RECONCILED",
        "CASH_MISMATCH",
    )


def test_cash_mismatch_does_not_block_risk_reducing_sell_planning(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    runtime.portfolio.apply_fill(
        Fill(
            fill_id="seed-position",
            order_intent_id="seed-intent",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=NOW - timedelta(minutes=5),
        )
    )
    result, _, _ = record_reconciliation(
        runtime,
        BrokerPortfolioTruth(
            cash=runtime.portfolio.cash - Decimal("10"),
            positions=(BrokerPositionTruth("AAPL", Decimal("1")),),
        ),
        at=NOW - timedelta(seconds=1),
    )
    assert result.reasons == ("CASH_MISMATCH",)

    _, intent, decision = runtime.paper_pipeline.plan(
        falling_bars(),
        decision_time=NOW,
        risk_context=risk_context(runtime, price="100"),
    )
    assert intent is not None and intent.side is Side.SELL
    assert decision is not None and decision.approved
