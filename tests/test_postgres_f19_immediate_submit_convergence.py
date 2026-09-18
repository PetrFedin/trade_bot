from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL F19 convergence tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.execution_facts import PostgresExecutionFactStore
from app.execution.paper_executor import PaperSubmitExecutor
from app.execution.trade_fills import (
    ExactBrokerFill,
    ExplicitZeroPaperFeeModel,
    PaperTradeFillAccounting,
)
from app.oms.indexed import IndexedPostgresOmsStore
from app.oms.store import OrderState
from app.portfolio.strict import StrictPostgresPortfolioEventStore
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskEvaluationMode,
    RiskLimits,
)
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
OPENING_CASH = Decimal("1000000")


class ImmediateFillBroker:
    paper_order_writes_enabled = True

    def __init__(self, *, broker_order_id: str) -> None:
        self.broker_order_id = broker_order_id
        self.submit_calls = 0

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        return BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=self.broker_order_id,
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.FILLED,
            filled_quantity=kwargs["quantity"],
            updated_at=NOW,
            filled_avg_price=Decimal("99"),
        )

    def get_order_by_client_order_id(self, client_order_id: str):
        return None


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


def test_postgres_immediate_submit_fill_converges_and_late_alias_is_not_double_counted() -> None:
    suffix = uuid4().hex
    intent_id = f"pg-f19-{suffix}"
    broker_order_id = f"pg-f19-broker-{suffix}"
    symbol = "F19PG"

    oms = IndexedPostgresOmsStore(DSN)
    oms.migrate()
    facts = PostgresExecutionFactStore(DSN)
    facts.migrate()
    portfolio = StrictPostgresPortfolioEventStore(DSN)
    portfolio.migrate()

    before = portfolio.replay(opening_cash=OPENING_CASH)
    before_quantity = before.position(symbol).quantity
    before_cash = before.cash
    runtime_ledger = portfolio.replay(opening_cash=OPENING_CASH)

    value = OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="pg-f19-convergence",
    )
    prepared = PaperOrderLifecycle(oms).prepare(
        value,
        approved(value),
        occurred_at=NOW,
    )
    message = oms.pending_outbox(limit=100)
    submit = next(item for item in message if item.intent_id == intent_id)

    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        execution_facts=facts,
        opening_cash=OPENING_CASH,
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=runtime_ledger,
    )
    broker = ImmediateFillBroker(broker_order_id=broker_order_id)
    result = PaperSubmitExecutor(
        store=oms,
        broker=broker,
        fill_accounting=accounting,
    ).execute(submit, occurred_at=NOW)

    assert result.record.state is OrderState.FILLED
    assert result.record.filled_quantity == Decimal("1")
    assert result.record.broker_order_id == broker_order_id
    assert broker.submit_calls == 1
    assert facts.unresolved_count() == 0

    projected = facts.projected(intent_id)
    assert len(projected) == 1
    assert projected[0].cumulative_quantity == Decimal("1")
    assert projected[0].quantity == Decimal("1")
    assert projected[0].price == Decimal("99")

    after_submit = portfolio.replay(opening_cash=OPENING_CASH)
    assert after_submit.position(symbol).quantity == before_quantity + Decimal("1")
    assert after_submit.cash == before_cash - Decimal("99")
    assert runtime_ledger.position(symbol).quantity == before_quantity + Decimal("1")
    assert runtime_ledger.cash == before_cash - Decimal("99")

    late_exact = ExactBrokerFill(
        execution_id=f"pg-f19-late-{suffix}",
        broker_order_id=broker_order_id,
        client_order_id=prepared.client_order_id,
        symbol=symbol,
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("99"),
        occurred_at=NOW + timedelta(seconds=1),
    )
    late = accounting.apply(intent_id, late_exact)
    assert late.portfolio_event_appended is False
    assert late.oms_advanced is False
    assert facts.unresolved_count() == 0

    after_late = portfolio.replay(opening_cash=OPENING_CASH)
    assert after_late.position(symbol).quantity == before_quantity + Decimal("1")
    assert after_late.cash == before_cash - Decimal("99")
    assert runtime_ledger.position(symbol).quantity == before_quantity + Decimal("1")
    assert runtime_ledger.cash == before_cash - Decimal("99")
