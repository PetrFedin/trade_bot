from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.application.bybit_product_composition import BybitProductStartupReconciler
from app.domain.trading import Side
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.execution.bybit_startup_reconciliation import BybitStartupReconciliationStatus
from app.oms.bybit_entry import bybit_entry_intent_id
from app.oms.store import OrderRecord, OrderState

NOW_MS = 1_800_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)
ORDER_LINK_ID = "ASTRA-DEMO-E-1234567890ABCDEF"


class _EntryOms:
    live_mainnet_order_routing_allowed = False

    def __init__(self, record: OrderRecord) -> None:
        self.record = record
        self.lifecycle_calls = 0
        self.rejected_calls = 0

    def unresolved_entry_submissions(self):
        return () if self.record.state is OrderState.REJECTED else (self.record,)

    def mark_lifecycle_reconciliation_required(
        self,
        intent_id,
        *,
        broker_order_id,
        broker_status,
        cumulative_executed_quantity,
        occurred_at,
    ):
        assert intent_id == self.record.intent_id
        assert occurred_at == NOW
        self.lifecycle_calls += 1
        self.record = replace(
            self.record,
            state=OrderState.RECONCILING,
            broker_order_id=broker_order_id,
        )
        return self.record

    def resolve_rejected_without_execution(
        self,
        intent_id,
        *,
        broker_order_id,
        cumulative_executed_quantity,
        occurred_at,
    ):
        assert intent_id == self.record.intent_id
        assert cumulative_executed_quantity == 0
        assert occurred_at == NOW
        self.rejected_calls += 1
        self.record = replace(
            self.record,
            state=OrderState.REJECTED,
            broker_order_id=broker_order_id,
        )
        return self.record


class _Broker:
    live_mainnet_order_routing_allowed = False

    def __init__(self, truth: BybitOrderTruth | None) -> None:
        self.truth = truth
        self.lookup_calls = 0

    def get_order_by_link_id(self, **_kwargs):
        self.lookup_calls += 1
        return self.truth

    def get_positions(self, *, settle_coin="USDT"):
        return ()

    def get_open_orders(self, *, settle_coin="USDT", limit=50):
        return ()


class _CheckpointStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def load(self):
        raise FileNotFoundError


def _record(state: OrderState = OrderState.UNCERTAIN) -> OrderRecord:
    return OrderRecord(
        intent_id=bybit_entry_intent_id(ORDER_LINK_ID),
        client_order_id=ORDER_LINK_ID,
        broker_order_id="",
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("60001"),
        filled_quantity=Decimal("0"),
        state=state,
        version=5,
        updated_at=NOW,
    )


def _truth(*, status: str, cumulative: str) -> BybitOrderTruth:
    return BybitOrderTruth(
        order_id="broker-1",
        order_link_id=ORDER_LINK_ID,
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        cumulative_executed_quantity=Decimal(cumulative),
        status=status,
        reject_reason="EC_NoError" if status != "Rejected" else "EC_NoEnoughQtyToFill",
    )


def _reconciler(oms: _EntryOms, broker: _Broker) -> BybitProductStartupReconciler:
    return BybitProductStartupReconciler(
        broker=broker,
        checkpoint_store=_CheckpointStore(),
        entry_oms=oms,
        clock_ms=lambda: NOW_MS,
    )


def test_missing_order_truth_remains_fail_closed() -> None:
    oms = _EntryOms(_record())
    result = _reconciler(oms, _Broker(None)).run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.next_entry_allowed is False
    assert result.broker_truth_complete is False
    assert any("BYBIT_OMS_ENTRY_NOT_FOUND_BY_ORDER_LINK_ID" in reason for reason in result.reasons)
    assert oms.record.state is OrderState.UNCERTAIN


def test_rejected_zero_execution_can_clear_submission_blocker() -> None:
    oms = _EntryOms(_record())
    broker = _Broker(_truth(status="Rejected", cumulative="0"))

    result = _reconciler(oms, broker).run()

    assert result.status is BybitStartupReconciliationStatus.READY_FOR_ENTRY
    assert result.next_entry_allowed is True
    assert result.broker_truth_complete is True
    assert oms.record.state is OrderState.REJECTED
    assert oms.rejected_calls == 1


def test_filled_order_is_adopted_for_lifecycle_reconciliation_but_stays_blocked() -> None:
    oms = _EntryOms(_record(state=OrderState.SUBMIT_STARTED))
    broker = _Broker(_truth(status="Filled", cumulative="0.01"))

    result = _reconciler(oms, broker).run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.next_entry_allowed is False
    assert result.broker_truth_complete is True
    assert oms.record.state is OrderState.RECONCILING
    assert oms.lifecycle_calls == 1
    assert any(
        "BYBIT_ENTRY_LIFECYCLE_RECONCILIATION_REQUIRED" in reason
        and "Filled" in reason
        for reason in result.reasons
    )


def test_cancelled_order_is_not_auto_treated_as_zero_execution_rejection() -> None:
    oms = _EntryOms(_record())
    broker = _Broker(_truth(status="Cancelled", cumulative="0"))

    result = _reconciler(oms, broker).run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert oms.record.state is OrderState.RECONCILING
    assert oms.lifecycle_calls == 1
    assert oms.rejected_calls == 0
