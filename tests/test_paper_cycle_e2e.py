from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.composition import ProductConfig, build_local_product
from app.application.paper_cycle import PaperCycleService
from app.domain.trading import Bar
from app.execution.trade_fills import ExplicitZeroPaperFeeModel
from app.oms.reconciliation import BrokerPortfolioTruth, BrokerPositionTruth
from app.oms.store import OrderState
from app.risk.pretrade import RiskLimits
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)
from app.runtime.paper_broker_contract_v99 import (
    BrokerOrder,
    BrokerOrderStatus,
)

NOW = datetime(2026, 8, 7, 18, 45, tzinfo=UTC)


def config(*, opening_cash: str = "10000") -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal(opening_cash),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def listening_stream() -> AlpacaTradeUpdateStreamV100:
    credentials = AlpacaPaperCredentialsV100(key_id="paper-key", secret_key="paper-secret")
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials)
    stream.authentication_frame()
    stream.ingest(
        json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
        received_at=NOW,
        expected_generation=1,
    )
    stream.ingest(
        json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
        received_at=NOW,
        expected_generation=1,
    )
    return stream


class FakeCycleBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-cycle-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        return order

    def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)


def fill_frame(*, client_order_id: str) -> str:
    return json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": "fill",
                "execution_id": "cycle-exec-1",
                "qty": "1",
                "price": "101",
                "timestamp": "2026-08-07T18:45:01Z",
                "order": {
                    "id": "broker-cycle-1",
                    "client_order_id": client_order_id,
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "1",
                    "limit_price": "102",
                    "status": "filled",
                    "filled_qty": "1",
                    "updated_at": "2026-08-07T18:45:01Z",
                },
            },
        }
    )


def build_cycle(tmp_path, broker: FakeCycleBroker):
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    cycle = PaperCycleService(
        runtime=runtime,
        broker=broker,
        trade_stream=listening_stream(),
        stream_generation=1,
    )
    return runtime, cycle


def test_bounded_paper_cycle_reaches_fill_portfolio_reconcile_and_restart(tmp_path) -> None:
    broker = FakeCycleBroker()
    runtime, cycle = build_cycle(tmp_path, broker)

    planning = cycle.plan_and_prepare(bars())
    assert planning.order_ready
    assert planning.risk is not None and planning.risk.approved
    assert planning.prepared is not None
    assert planning.prepared.record.state is OrderState.OUTBOXED
    assert len(runtime.risk_admission.journal.verify()) == 1

    execution = cycle.execute_next_submit(occurred_at=NOW)
    assert execution is not None
    assert execution.mutation_attempted
    assert execution.record.state is OrderState.ACKNOWLEDGED
    assert broker.submit_calls == 1
    assert cycle.execute_next_submit(occurred_at=NOW) is None

    processed = cycle.process_trade_update(
        fill_frame(client_order_id=planning.prepared.client_order_id),
        received_at=NOW + timedelta(seconds=1),
    )
    assert processed.fill_accounting is not None
    assert processed.fill_accounting.record.state is OrderState.FILLED
    assert runtime.portfolio.position("AAPL").quantity == Decimal("1")
    assert runtime.portfolio.position("AAPL").average_cost == Decimal("101")
    assert runtime.portfolio.cash == Decimal("9899")

    reconciliation = cycle.reconcile_portfolio(
        BrokerPortfolioTruth(
            cash=Decimal("9899"),
            positions=(BrokerPositionTruth("AAPL", Decimal("1")),),
        )
    )
    assert reconciliation.matched

    no_rebalance = cycle.plan_and_prepare(bars())
    assert no_rebalance.intent is None
    assert not no_rebalance.order_ready

    restarted_runtime, restarted_cycle = build_cycle(tmp_path, broker)
    assert restarted_runtime.portfolio.cash == Decimal("9899")
    assert restarted_runtime.portfolio.position("AAPL").quantity == Decimal("1")
    after_restart = restarted_cycle.plan_and_prepare(bars())
    assert after_restart.intent is None
    assert broker.submit_calls == 1


def test_cycle_rejects_buy_above_replayed_available_cash_before_outbox(tmp_path) -> None:
    runtime = build_local_product(
        config=config(opening_cash="50"),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    broker = FakeCycleBroker()
    cycle = PaperCycleService(
        runtime=runtime,
        broker=broker,
        trade_stream=listening_stream(),
        stream_generation=1,
    )

    planning = cycle.plan_and_prepare(bars())
    assert planning.intent is not None
    assert planning.risk is not None and not planning.risk.approved
    assert planning.risk.reasons == ("INSUFFICIENT_AVAILABLE_CASH",)
    assert planning.prepared is None
    assert runtime.oms_store.pending_outbox() == ()
    assert broker.submit_calls == 0
    evidence = runtime.risk_admission.journal.verify()
    assert len(evidence) == 1
    assert evidence[0].payload["decision"]["approved"] is False
