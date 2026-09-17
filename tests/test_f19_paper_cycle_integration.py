from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.oms.store import OrderState
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus
from tests.test_paper_cycle_e2e import (
    NOW,
    FakeCycleBroker,
    bars,
    build_cycle,
    fill_frame,
    operational_context,
)


class ImmediateCycleFillBroker(FakeCycleBroker):
    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-cycle-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.FILLED,
            filled_quantity=Decimal("1"),
            filled_avg_price=Decimal("101"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        return order


def test_paper_cycle_immediate_fill_waits_for_exact_trade_update_before_accounting(
    tmp_path,
) -> None:
    broker = ImmediateCycleFillBroker()
    runtime, cycle = build_cycle(tmp_path, broker)
    planning = cycle.plan_and_prepare(
        bars(),
        decision_time=NOW,
        risk_context=operational_context(runtime),
    )
    assert planning.prepared is not None

    execution = cycle.execute_next_submit(occurred_at=NOW)
    assert execution is not None
    assert execution.record.state is OrderState.ACKNOWLEDGED
    assert execution.record.filled_quantity == Decimal("0")
    assert broker.submit_calls == 1
    assert runtime.execution_checkpoints.unresolved_count() == 1
    assert runtime.portfolio.position("AAPL").quantity == Decimal("0")

    processed = cycle.process_trade_update(
        fill_frame(client_order_id=planning.prepared.client_order_id),
        received_at=NOW + timedelta(seconds=1),
    )
    assert processed.fill_accounting is not None
    assert processed.fill_accounting.record.state is OrderState.FILLED
    assert processed.fill_accounting.record.filled_quantity == Decimal("1")
    assert runtime.execution_checkpoints.unresolved_count() == 0
    assert runtime.execution_facts.unresolved_count() == 0
    assert runtime.portfolio.position("AAPL").quantity == Decimal("1")
    assert runtime.portfolio.cash == Decimal("9899")
