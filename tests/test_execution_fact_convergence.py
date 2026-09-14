from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.domain.trading import Bar, OrderIntent, Side
from app.execution.execution_facts import (
    ExecutionFact,
    ExecutionProjectionState,
    SQLiteExecutionFactStore,
)
from app.execution.trade_fills import ExactBrokerFill, canonical_broker_fill_id
from app.oms.store import OrderState
from app.risk.pretrade import OperationalRiskContext, RiskLimits

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class FixedFee:
    def __init__(self, fee: str) -> None:
        self.fee = Decimal(fee)

    def fee_for(self, fill: ExactBrokerFill) -> Decimal:
        fill.validate()
        return self.fee


def config(*, opening_cash: str = "102") -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal(opening_cash),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("1000"),
            maximum_gross_notional=Decimal("1000"),
            maximum_position_fraction_of_equity=Decimal("1"),
            maximum_sector_fraction_of_equity=Decimal("1"),
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def risk_context(runtime) -> OperationalRiskContext:
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
        portfolio_equity=runtime.portfolio.cash,
        sector_notional=Decimal("0"),
        annualized_volatility=Decimal("0.20"),
        available_cash=runtime.portfolio.cash,
    )


def prepare_ack(runtime) -> OrderIntent:
    target, intent, decision = runtime.paper_pipeline.plan(
        bars(), decision_time=NOW, risk_context=risk_context(runtime)
    )
    assert target.quantity == Decimal("1")
    assert intent is not None
    assert decision is not None and decision.approved
    prepared = runtime.order_lifecycle.prepare(intent, decision, occurred_at=NOW)
    runtime.oms_store.transition(
        intent.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-start",
        occurred_at=NOW,
    )
    runtime.oms_store.transition(
        intent.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="ack",
        occurred_at=NOW,
        broker_order_id="broker-1",
    )
    assert prepared.client_order_id == runtime.oms_store.get(intent.intent_id).client_order_id
    return intent


def exact_fill(runtime, intent: OrderIntent, *, execution_id: str = "exec-1") -> ExactBrokerFill:
    record = runtime.oms_store.get(intent.intent_id)
    assert record is not None
    return ExactBrokerFill(
        execution_id=execution_id,
        broker_order_id="broker-1",
        client_order_id=record.client_order_id,
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("102"),
        occurred_at=NOW + timedelta(seconds=1),
    )


def test_execution_fact_survives_accounting_failure_and_blocks_new_risk(tmp_path: Path) -> None:
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=FixedFee("0.10"),
    )
    intent = prepare_ack(runtime)
    fill = exact_fill(runtime, intent)

    with pytest.raises(ValueError, match="INSUFFICIENT_CASH"):
        runtime.require_fill_accounting().apply(intent.intent_id, fill)

    record = runtime.oms_store.get(intent.intent_id)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert record.filled_quantity == Decimal("1")

    unresolved = runtime.execution_facts.unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].state is ExecutionProjectionState.QUARANTINED
    assert unresolved[0].fact.execution_fact_id == canonical_broker_fill_id(fill)
    assert unresolved[0].reason == "INSUFFICIENT_CASH"

    replayed = runtime.portfolio_store.replay(opening_cash=Decimal("102"))
    assert replayed.cash == Decimal("102")
    assert replayed.position("AAPL").quantity == Decimal("0")

    with pytest.raises(RuntimeError, match="EXECUTION_ACCOUNTING_NOT_CONVERGED"):
        runtime.paper_pipeline.plan(bars(), decision_time=NOW)

    restarted = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=FixedFee("0.10"),
    )
    assert restarted.execution_facts.unresolved_count() == 1
    with pytest.raises(RuntimeError, match="EXECUTION_ACCOUNTING_NOT_CONVERGED"):
        restarted.paper_pipeline.plan(bars(), decision_time=NOW)


def test_pending_fact_recovers_idempotently_after_restart(tmp_path: Path) -> None:
    runtime = build_local_product(
        config=config(opening_cash="1000"),
        state_directory=tmp_path,
        fee_provider=FixedFee("0.10"),
    )
    intent = prepare_ack(runtime)
    fill = exact_fill(runtime, intent)
    fact = ExecutionFact(
        execution_fact_id=canonical_broker_fill_id(fill),
        intent_id=intent.intent_id,
        broker_order_id=fill.broker_order_id,
        client_order_id=fill.client_order_id,
        symbol=fill.symbol,
        side=fill.side,
        order_quantity=fill.order_quantity,
        cumulative_quantity=fill.cumulative_quantity,
        quantity=fill.quantity,
        price=fill.price,
        fee=Decimal("0.10"),
        occurred_at=fill.occurred_at,
    )
    runtime.execution_facts.append(fact, source_execution_id=fill.execution_id)
    assert runtime.execution_facts.unresolved_count() == 1

    restarted = build_local_product(
        config=config(opening_cash="1000"),
        state_directory=tmp_path,
        fee_provider=FixedFee("0.10"),
    )
    results = restarted.require_fill_accounting().recover_unresolved()
    assert len(results) == 1
    assert restarted.execution_facts.unresolved_count() == 0
    assert restarted.portfolio.position("AAPL").quantity == Decimal("1")
    assert restarted.portfolio.cash == Decimal("897.90")
    assert restarted.oms_store.get(intent.intent_id).state is OrderState.FILLED

    assert restarted.require_fill_accounting().recover_unresolved() == ()
    replayed = restarted.portfolio_store.replay(opening_cash=Decimal("1000"))
    assert replayed.position("AAPL").quantity == Decimal("1")
    assert replayed.cash == Decimal("897.90")


def test_execution_source_id_conflict_is_fail_closed(tmp_path: Path) -> None:
    store = SQLiteExecutionFactStore(tmp_path / "execution.sqlite")
    base = ExecutionFact(
        execution_fact_id="fact-a",
        intent_id="intent-a",
        broker_order_id="broker-a",
        client_order_id="client-a",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        occurred_at=NOW,
    )
    store.append(base, source_execution_id="source-1")
    conflicting = ExecutionFact(
        execution_fact_id="fact-b",
        intent_id="intent-b",
        broker_order_id="broker-b",
        client_order_id="client-b",
        symbol="MSFT",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("200"),
        fee=Decimal("0"),
        occurred_at=NOW,
    )
    with pytest.raises(ValueError, match="EXECUTION_SOURCE_ID_CONFLICT"):
        store.append(conflicting, source_execution_id="source-1")


def test_projected_fact_cannot_regress_to_quarantined(tmp_path: Path) -> None:
    store = SQLiteExecutionFactStore(tmp_path / "execution.sqlite")
    fact = ExecutionFact(
        execution_fact_id="fact-projected",
        intent_id="intent-projected",
        broker_order_id="broker-projected",
        client_order_id="client-projected",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        occurred_at=NOW,
    )
    store.append(fact, source_execution_id="source-projected")
    store.mark_projected(fact.execution_fact_id, occurred_at=NOW)

    with pytest.raises(ValueError, match="EXECUTION_PROJECTION_STATE_REGRESSION"):
        store.mark_quarantined(
            fact.execution_fact_id,
            reason="late failure",
            occurred_at=NOW + timedelta(seconds=1),
        )

    assert store.unresolved_count() == 0
