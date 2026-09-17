from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import Bar, OrderIntent, Side
from app.execution.execution_checkpoints import (
    ExecutionCheckpoint,
    SQLiteExecutionCheckpointStore,
    canonical_execution_checkpoint_id,
)
from app.execution.execution_facts import ExecutionFact, SQLiteExecutionFactStore
from app.execution.trade_fills import ExactBrokerFill, ExplicitZeroPaperFeeModel
from app.oms.store import OrderState
from app.risk.pretrade import (
    OperationalRiskContext,
    PreTradeRiskEngine,
    RiskEvaluationMode,
    RiskLimits,
)
from app.runtime.paper_broker_contract_v99 import (
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
)

NOW = datetime(2026, 9, 17, 11, 0, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("1000"),
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
    marks = {"AAPL": Decimal("102")}
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
        sector_notional=Decimal("0"),
        annualized_volatility=Decimal("0.20"),
        available_cash=runtime.portfolio.cash,
        portfolio_mark_prices=marks,
    )


def plan_and_prepare(runtime):
    _, intent, decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=risk_context(runtime),
    )
    assert intent is not None
    assert decision is not None and decision.approved
    runtime.order_lifecycle.prepare(intent, decision, occurred_at=NOW)
    message = next(
        item for item in runtime.oms_store.pending_outbox() if item.intent_id == intent.intent_id
    )
    return intent, message


def approved(value: OrderIntent):
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("10000"),
            maximum_symbol_notional=Decimal("10000"),
            maximum_gross_notional=Decimal("10000"),
        )
    ).evaluate(
        value,
        mode=RiskEvaluationMode.REPLAY,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
    )


class ImmediateFillBroker:
    paper_order_writes_enabled = True

    def __init__(self, *, status: BrokerOrderStatus, filled_quantity: Decimal) -> None:
        self.status = status
        self.filled_quantity = filled_quantity
        self.submit_calls = 0
        self.get_calls = 0
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-f19",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=self.status,
            filled_quantity=self.filled_quantity,
            filled_avg_price=Decimal("102") if self.filled_quantity > 0 else None,
            updated_at=NOW + timedelta(seconds=1),
        )
        self.orders[order.client_order_id] = order
        return order

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.orders.get(client_order_id)


def exact_fill(runtime, intent: OrderIntent) -> ExactBrokerFill:
    record = runtime.oms_store.get(intent.intent_id)
    assert record is not None
    return ExactBrokerFill(
        execution_id="f19-exec-1",
        broker_order_id="broker-f19",
        client_order_id=record.client_order_id,
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("102"),
        occurred_at=NOW + timedelta(seconds=2),
    )


def test_immediate_filled_submit_creates_durable_barrier_then_exact_fill_converges(
    tmp_path,
) -> None:
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    intent, message = plan_and_prepare(runtime)
    broker = ImmediateFillBroker(
        status=BrokerOrderStatus.FILLED,
        filled_quantity=Decimal("1"),
    )

    result = runtime.build_submit_executor(broker).execute(message, occurred_at=NOW)

    assert result.record.state is OrderState.ACKNOWLEDGED
    assert result.record.filled_quantity == Decimal("0")
    assert runtime.execution_checkpoints.unresolved_count() == 1
    checkpoint = runtime.execution_checkpoints.unresolved()[0]
    assert checkpoint.cumulative_quantity == Decimal("1")
    assert checkpoint.observed_avg_price == Decimal("102")
    assert runtime.execution_facts.unresolved_count() == 0
    assert runtime.portfolio.position("AAPL").quantity == Decimal("0")
    assert runtime.portfolio.cash == Decimal("1000")

    with pytest.raises(RuntimeError, match="EXECUTION_ACCOUNTING_NOT_CONVERGED"):
        runtime.paper_pipeline.plan(
            bars(),
            decision_time=NOW,
            risk_context=risk_context(runtime),
        )

    accounting = runtime.require_fill_accounting().apply(intent.intent_id, exact_fill(runtime, intent))
    assert accounting.record.state is OrderState.FILLED
    assert accounting.record.filled_quantity == Decimal("1")
    assert runtime.execution_checkpoints.unresolved_count() == 0
    assert runtime.execution_facts.unresolved_count() == 0
    assert runtime.portfolio.position("AAPL").quantity == Decimal("1")
    assert runtime.portfolio.cash == Decimal("898")

    _, next_intent, next_decision = runtime.paper_pipeline.plan(
        bars(),
        decision_time=NOW,
        risk_context=risk_context(runtime),
    )
    assert next_intent is None
    assert next_decision is None


def test_unresolved_execution_blocks_already_prepared_second_buy_before_post(tmp_path) -> None:
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    first_intent, first_message = plan_and_prepare(runtime)

    second_intent = OrderIntent(
        intent_id="f19-second-buy",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f19-second",
    )
    PaperOrderLifecycle(runtime.oms_store).prepare(
        second_intent,
        approved(second_intent),
        occurred_at=NOW,
    )
    second_message = next(
        item
        for item in runtime.oms_store.pending_outbox()
        if item.intent_id == second_intent.intent_id
    )

    first_broker = ImmediateFillBroker(
        status=BrokerOrderStatus.FILLED,
        filled_quantity=Decimal("1"),
    )
    runtime.build_submit_executor(first_broker).execute(first_message, occurred_at=NOW)
    assert runtime.execution_checkpoints.unresolved_count() == 1
    assert runtime.oms_store.get(first_intent.intent_id).filled_quantity == Decimal("0")  # type: ignore[union-attr]

    second_broker = ImmediateFillBroker(
        status=BrokerOrderStatus.ACKNOWLEDGED,
        filled_quantity=Decimal("0"),
    )
    with pytest.raises(RuntimeError, match="EXECUTION_ACCOUNTING_NOT_CONVERGED"):
        runtime.build_submit_executor(second_broker).execute(
            second_message,
            occurred_at=NOW + timedelta(seconds=2),
        )
    assert second_broker.submit_calls == 0
    second_record = runtime.oms_store.get(second_intent.intent_id)
    assert second_record is not None and second_record.state is OrderState.OUTBOXED


def test_checkpoint_survives_restart_until_exact_execution_projects(tmp_path) -> None:
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    intent, message = plan_and_prepare(runtime)
    broker = ImmediateFillBroker(
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_quantity=Decimal("1"),
    )
    runtime.build_submit_executor(broker).execute(message, occurred_at=NOW)

    restarted = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    assert restarted.execution_checkpoints.unresolved_count() == 1
    assert restarted.portfolio.position("AAPL").quantity == Decimal("0")
    with pytest.raises(RuntimeError, match="EXECUTION_ACCOUNTING_NOT_CONVERGED"):
        restarted.paper_pipeline.plan(
            bars(),
            decision_time=NOW,
            risk_context=risk_context(restarted),
        )

    restarted.require_fill_accounting().apply(intent.intent_id, exact_fill(restarted, intent))
    assert restarted.execution_checkpoints.unresolved_count() == 0
    replayed = restarted.portfolio_store.replay(opening_cash=Decimal("1000"))
    assert replayed.position("AAPL").quantity == Decimal("1")
    assert replayed.cash == Decimal("898")


def test_projected_execution_closes_checkpoint_created_after_stream_projection(tmp_path) -> None:
    execution_path = tmp_path / "execution.sqlite"
    facts = SQLiteExecutionFactStore(execution_path)
    checkpoints = SQLiteExecutionCheckpointStore(execution_path)
    fact = ExecutionFact(
        execution_fact_id="projected-before-submit-response",
        intent_id="race-intent",
        broker_order_id="broker-race",
        client_order_id="client-race",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("102"),
        fee=Decimal("0"),
        occurred_at=NOW,
    )
    facts.append(fact, source_execution_id="race-source")
    facts.mark_projected(fact.execution_fact_id, occurred_at=NOW)

    checkpoint = ExecutionCheckpoint(
        checkpoint_id=canonical_execution_checkpoint_id(
            intent_id="race-intent",
            broker_order_id="broker-race",
            cumulative_quantity=Decimal("1"),
            observed_at=NOW + timedelta(seconds=1),
        ),
        intent_id="race-intent",
        broker_order_id="broker-race",
        client_order_id="client-race",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        broker_status=BrokerOrderStatus.FILLED.value,
        observed_avg_price=Decimal("102"),
        observed_at=NOW + timedelta(seconds=1),
    )
    checkpoints.append(checkpoint)
    assert checkpoints.unresolved_count() == 1
    assert checkpoints.resolve_from_projected_facts(
        intent_id="race-intent",
        occurred_at=NOW + timedelta(seconds=2),
    ) == 1
    assert checkpoints.unresolved_count() == 0


def test_checkpoint_replay_is_idempotent_and_payload_conflict_fails_closed(tmp_path) -> None:
    store = SQLiteExecutionCheckpointStore(tmp_path / "checkpoint.sqlite")
    observed = NOW + timedelta(seconds=1)
    checkpoint = ExecutionCheckpoint(
        checkpoint_id=canonical_execution_checkpoint_id(
            intent_id="checkpoint-intent",
            broker_order_id="broker-checkpoint",
            cumulative_quantity=Decimal("0.5"),
            observed_at=observed,
        ),
        intent_id="checkpoint-intent",
        broker_order_id="broker-checkpoint",
        client_order_id="client-checkpoint",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("0.5"),
        broker_status=BrokerOrderStatus.PARTIALLY_FILLED.value,
        observed_avg_price=Decimal("101"),
        observed_at=observed,
    )
    assert store.append(checkpoint)
    assert not store.append(checkpoint)

    conflicting = ExecutionCheckpoint(
        **{
            **checkpoint.__dict__,
            "broker_status": BrokerOrderStatus.FILLED.value,
        }
    )
    with pytest.raises(ValueError, match="EXECUTION_CHECKPOINT_CONFLICT"):
        store.append(conflicting)
