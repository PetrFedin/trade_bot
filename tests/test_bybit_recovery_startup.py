from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.application.bybit_recovery_startup as startup_module
from app.application.bybit_recovery_startup import RecoveryAwareBybitProductStartupReconciler
from app.domain.trading import Side
from app.execution.bybit_entry_recovery_convergence import (
    BybitEntryRecoveryConvergenceResult,
    BybitEntryRecoveryConvergenceStatus,
)
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)
from app.oms.store import OrderRecord, OrderState

NOW = datetime(2026, 8, 21, 17, 15, tzinfo=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-STARTUP-RECOVERY-01"


class _Base:
    live_mainnet_order_routing_allowed = False

    def __init__(self, result: BybitStartupReconciliationResult) -> None:
        self.result = result
        self.calls = 0

    def run(self) -> BybitStartupReconciliationResult:
        self.calls += 1
        return self.result


class _Candidates:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, records: tuple[OrderRecord, ...]) -> None:
        self.records = records

    def load_candidates(self, *, limit: int = 8) -> tuple[OrderRecord, ...]:
        assert limit == 8
        return self.records


class _Broker:
    live_mainnet_order_routing_allowed = False

    def __init__(self, truth: BybitOrderTruth | None, positions=()) -> None:
        self.truth = truth
        self.positions = positions
        self.order_reads = 0

    def get_order_by_link_id(self, **kwargs):
        self.order_reads += 1
        assert kwargs["order_link_id"] == ENTRY_LINK
        return self.truth

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        return self.positions


class _CheckpointStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, checkpoint=None) -> None:
        self.checkpoint = checkpoint

    def load(self):
        if self.checkpoint is None:
            raise FileNotFoundError
        return self.checkpoint


class _SafeDependency:
    live_mainnet_order_routing_allowed = False


class _Health:
    def __init__(self) -> None:
        self.results = []

    def record(self, result, *, observed_monotonic) -> None:
        self.results.append((result, observed_monotonic))


def _record(state: OrderState, *, suffix: str = "") -> OrderRecord:
    link = f"{ENTRY_LINK}{suffix}"
    return OrderRecord(
        intent_id=f"bybit-entry:{link}",
        client_order_id=link,
        broker_order_id="broker-1" if state is not OrderState.SUBMIT_STARTED else "",
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("100000"),
        filled_quantity=Decimal("0.01") if state is OrderState.FILLED else Decimal("0"),
        state=state,
        version=5,
        updated_at=NOW,
    )


def _truth(*, status: str = "Filled", executed: str = "0.01") -> BybitOrderTruth:
    return BybitOrderTruth(
        order_id="broker-1",
        order_link_id=ENTRY_LINK,
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        cumulative_executed_quantity=Decimal(executed),
        status=status,
        reject_reason="EC_NoError",
    )


def _startup(status: BybitStartupReconciliationStatus) -> BybitStartupReconciliationResult:
    return BybitStartupReconciliationResult(
        status=status,
        reasons=(status.value,),
        checkpoint=None,
        active_positions=(),
        open_orders=(),
        next_entry_allowed=status is BybitStartupReconciliationStatus.READY_FOR_ENTRY,
        management_allowed=status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        terminal_recovery_required=(
            status is BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED
        ),
        broker_truth_complete=True,
    )


def _wrapper(*, base, candidates, broker, checkpoint=None, health=None):
    candidate_reader = _Candidates(candidates)
    recovery_store = _SafeDependency()
    recovery_store.order_writes_supported = False
    recovery_store.immutable_records = True
    runtime_lease = _SafeDependency()
    runtime_lease.order_writes_supported = False
    runtime_lease.automatic_stale_takeover_allowed = False
    excursion_store = _SafeDependency()
    excursion_store.order_writes_supported = False
    entry_oms = _SafeDependency()
    recovery_client = _SafeDependency()
    return RecoveryAwareBybitProductStartupReconciler(
        base_reconciler=base,
        broker=broker,
        checkpoint_store=_CheckpointStore(checkpoint),
        candidate_reader=candidate_reader,
        recovery_store=recovery_store,
        runtime_lease=runtime_lease,
        excursion_store=excursion_store,
        entry_oms=entry_oms,
        recovery_client=recovery_client,
        reconciliation_health=health,
        clock_ms=lambda: int(NOW.timestamp() * 1000),
        monotonic_fn=lambda: 10.5,
    )


def test_matching_filled_candidate_with_existing_checkpoint_is_already_handed_to_management() -> None:
    base = _Base(_startup(BybitStartupReconciliationStatus.RESUME_MANAGEMENT))
    broker = _Broker(None)
    wrapper = _wrapper(
        base=base,
        candidates=(_record(OrderState.FILLED),),
        broker=broker,
        checkpoint=SimpleNamespace(entry_order_link_id=ENTRY_LINK),
    )

    result = wrapper.run()

    assert result.status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT
    assert base.calls == 1
    assert broker.order_reads == 0


def test_executed_submit_started_candidate_converges_then_delegates_to_normal_startup(
    monkeypatch,
) -> None:
    base = _Base(_startup(BybitStartupReconciliationStatus.RESUME_MANAGEMENT))
    broker = _Broker(_truth(), positions=(SimpleNamespace(size=Decimal("0.01")),))
    calls = []

    def _converge(record, **kwargs):
        calls.append((record, kwargs))
        return BybitEntryRecoveryConvergenceResult(
            status=BybitEntryRecoveryConvergenceStatus.ACTIVE_MANAGEMENT_READY,
            reasons=(),
            checkpoint=None,
            safety_result=None,
            oms_record=None,
            stale_lease_recovered=True,
            runtime_lease_acquired=True,
            runtime_lease_released=True,
        )

    monkeypatch.setattr(startup_module, "converge_bybit_executed_entry_recovery", _converge)
    health = _Health()
    wrapper = _wrapper(
        base=base,
        candidates=(_record(OrderState.SUBMIT_STARTED),),
        broker=broker,
        health=health,
    )

    result = wrapper.run()

    assert result.status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT
    assert len(calls) == 1
    assert calls[0][1]["order_truth"].status == "Filled"
    assert base.calls == 1
    assert len(health.results) == 1


def test_recovery_convergence_blocker_never_falls_through_to_normal_startup(monkeypatch) -> None:
    base = _Base(_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY))
    broker = _Broker(_truth(), positions=(SimpleNamespace(size=Decimal("0.01")),))

    monkeypatch.setattr(
        startup_module,
        "converge_bybit_executed_entry_recovery",
        lambda *_args, **_kwargs: BybitEntryRecoveryConvergenceResult(
            status=BybitEntryRecoveryConvergenceStatus.BLOCKED,
            reasons=("RECOVERY_RUNTIME_LEASE_UNAVAILABLE:RuntimeError",),
            checkpoint=None,
            safety_result=None,
            oms_record=None,
            stale_lease_recovered=False,
            runtime_lease_acquired=False,
            runtime_lease_released=False,
        ),
    )
    wrapper = _wrapper(
        base=base,
        candidates=(_record(OrderState.ACKNOWLEDGED),),
        broker=broker,
    )

    result = wrapper.run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.next_entry_allowed is False
    assert result.reasons == (
        "ENTRY_RECOVERY_BLOCKED:bybit-entry:ASTRA-DEMO-E-STARTUP-RECOVERY-01:"
        "RECOVERY_RUNTIME_LEASE_UNAVAILABLE:RuntimeError",
    )
    assert base.calls == 0


def test_multiple_unhanded_candidates_block_without_broker_or_mutation() -> None:
    base = _Base(_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY))
    broker = _Broker(_truth())
    wrapper = _wrapper(
        base=base,
        candidates=(
            _record(OrderState.SUBMIT_STARTED),
            _record(OrderState.ACKNOWLEDGED, suffix="-2"),
        ),
        broker=broker,
    )

    result = wrapper.run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.next_entry_allowed is False
    assert result.reasons[0].startswith("ENTRY_RECOVERY_MULTIPLE_UNHANDED_CANDIDATES:")
    assert broker.order_reads == 0
    assert base.calls == 0


def test_broker_rejection_after_ack_is_drift_not_auto_rejected() -> None:
    base = _Base(_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY))
    rejected = BybitOrderTruth(
        order_id="broker-1",
        order_link_id=ENTRY_LINK,
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        cumulative_executed_quantity=Decimal("0"),
        status="Rejected",
        reject_reason="EC_TooLateToCancel",
    )
    wrapper = _wrapper(
        base=base,
        candidates=(_record(OrderState.ACKNOWLEDGED),),
        broker=_Broker(rejected),
    )

    result = wrapper.run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == (
        "ENTRY_RECOVERY_BROKER_REJECTED_AFTER_ACK:"
        "bybit-entry:ASTRA-DEMO-E-STARTUP-RECOVERY-01",
    )
    assert base.calls == 0
