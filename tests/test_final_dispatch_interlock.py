from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.application.paper_cycle import PaperCycleService
from app.domain.trading import Bar
from app.execution.trade_fills import ExplicitZeroPaperFeeModel
from app.observability.readiness import OperationalSnapshot
from app.oms.store import OrderState
from app.risk.pretrade import RiskLimits
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus
from app.runtime.paper_dispatch_control import DispatchBlocked, DispatchControlMode

NOW = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("1000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("200"),
            maximum_symbol_notional=Decimal("200"),
            maximum_gross_notional=Decimal("200"),
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


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


def listening_stream() -> AlpacaTradeUpdateStreamV100:
    credentials = AlpacaPaperCredentialsV100(
        key_id="paper-key",
        secret_key="paper-secret",
    )
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


class MutableSnapshotProvider:
    def __init__(self, snapshot: OperationalSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def __call__(self) -> OperationalSnapshot:
        self.calls += 1
        return self.snapshot


class CountingBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.get_calls = 0
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=f"dispatch-broker-{self.submit_calls}",
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

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.orders.get(client_order_id)


def build_cycle(tmp_path, broker: CountingBroker, provider=None):
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
        operational_snapshot_provider=provider,
    )
    return runtime, cycle


def test_readiness_failure_after_outbox_durably_halts_before_broker_submit(tmp_path) -> None:
    broker = CountingBroker()
    provider = MutableSnapshotProvider(ready_snapshot())
    runtime, cycle = build_cycle(tmp_path, broker, provider)
    runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="qualification arm",
        occurred_at=NOW,
    )
    planning = cycle.plan_and_prepare(bars(), decision_time=NOW)
    assert planning.prepared is not None
    assert planning.prepared.record.state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1

    provider.snapshot = replace(ready_snapshot(), kill_switch_engaged=True)
    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=1))
    assert blocked.value.reasons == ("KILL_SWITCH_ENGAGED",)
    assert broker.submit_calls == 0
    assert broker.get_calls == 0
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1
    assert runtime.dispatch_control.current().mode is DispatchControlMode.HALTED
    event_types = [str(event["event_type"]) for event in runtime.dispatch_control.events()]
    assert event_types == ["ARM", "HALT"]

    restarted_provider = MutableSnapshotProvider(ready_snapshot())
    restarted_runtime, restarted_cycle = build_cycle(
        tmp_path,
        broker,
        restarted_provider,
    )
    assert restarted_runtime.dispatch_control.current().mode is DispatchControlMode.HALTED
    with pytest.raises(DispatchBlocked) as still_blocked:
        restarted_cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=2))
    assert still_blocked.value.reasons == ("DISPATCH_CONTROL_HALTED",)
    assert broker.submit_calls == 0
    assert len(restarted_runtime.oms_store.pending_outbox()) == 1

    restarted_runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="explicit re-arm after investigation",
        occurred_at=NOW + timedelta(seconds=3),
    )
    execution = restarted_cycle.execute_next_submit(
        occurred_at=NOW + timedelta(seconds=4)
    )
    assert execution is not None and execution.mutation_attempted
    assert execution.record.state is OrderState.ACKNOWLEDGED
    assert broker.submit_calls == 1
    assert restarted_runtime.oms_store.pending_outbox() == ()
    assert [
        str(event["event_type"]) for event in restarted_runtime.dispatch_control.events()
    ][-2:] == ["ARM", "DISPATCH_AUTHORIZED"]


def test_missing_operational_snapshot_fails_closed_and_persists_halt(tmp_path) -> None:
    broker = CountingBroker()
    runtime, cycle = build_cycle(tmp_path, broker)
    runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="qualification arm",
        occurred_at=NOW,
    )
    planning = cycle.plan_and_prepare(bars(), decision_time=NOW)
    assert planning.prepared is not None

    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=1))
    assert blocked.value.reasons == ("OPERATIONAL_SNAPSHOT_REQUIRED",)
    assert broker.submit_calls == 0
    assert runtime.dispatch_control.current().mode is DispatchControlMode.HALTED
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED


def test_expired_arm_cannot_authorize_outbox(tmp_path) -> None:
    broker = CountingBroker()
    provider = MutableSnapshotProvider(ready_snapshot())
    runtime, cycle = build_cycle(tmp_path, broker, provider)
    runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="short qualification arm",
        occurred_at=NOW,
        ttl=timedelta(seconds=1),
    )
    planning = cycle.plan_and_prepare(bars(), decision_time=NOW)
    assert planning.prepared is not None

    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=2))
    assert blocked.value.reasons == ("DISPATCH_CONTROL_EXPIRED",)
    assert broker.submit_calls == 0
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1
