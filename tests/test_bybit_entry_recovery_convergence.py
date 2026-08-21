from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.execution.bybit_entry_recovery_convergence as convergence_module
from app.domain.trading import Side
from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoPosition
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionCheckpoint
from app.execution.bybit_entry_recovery import (
    BybitEntryRecoveryEnvelope,
    BybitEntryRecoveryRecord,
    encode_entry_recovery_envelope,
)
from app.execution.bybit_entry_recovery_convergence import (
    BybitEntryRecoveryConvergenceStatus,
    converge_bybit_executed_entry_recovery,
)
from app.execution.bybit_entry_restart_recovery import (
    BybitExecutedEntryRecoveryResult,
    BybitExecutedEntryRecoveryStatus,
)
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.oms.store import OrderRecord, OrderState
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

NOW = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-CONVERGE-01"
INTENT_ID = f"bybit-entry:{ENTRY_LINK}"


def _trade_plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-21T15:55:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("0.01"),
        risk_budget_usdt=Decimal("20"),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal("1.10"),
        estimated_stop_loss_after_cost_usdt=Decimal("11.10"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0211"),
        expected_move_fraction=Decimal("0.05"),
        expected_net_edge_usd=Decimal("48.90"),
        quality_score=Decimal("0.92"),
    )


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("100"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _recovery_record() -> BybitEntryRecoveryRecord:
    envelope = BybitEntryRecoveryEnvelope(
        entry_order_link_id=ENTRY_LINK,
        order_side="Buy",
        approved_order_quantity=Decimal("0.01"),
        trade_plan=_trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(taker_fee_rate=Decimal("0.00055")),
        planned_exit_mode="FIXED_20_TARGET",
    )
    _canonical, record_sha = encode_entry_recovery_envelope(envelope)
    return BybitEntryRecoveryRecord(envelope=envelope, record_sha256=record_sha)


def _truth(*, status: str = "Filled", executed: str = "0.01") -> BybitOrderTruth:
    return BybitOrderTruth(
        order_id="broker-entry-1",
        order_link_id=ENTRY_LINK,
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        cumulative_executed_quantity=Decimal(executed),
        status=status,
        reject_reason="EC_NoError",
    )


def _position(*, size: str = "0.01") -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal("100000"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50000"),
    )


def _oms_record(*, state: OrderState = OrderState.SUBMIT_STARTED) -> OrderRecord:
    return OrderRecord(
        intent_id=INTENT_ID,
        client_order_id=ENTRY_LINK,
        broker_order_id="" if state is OrderState.SUBMIT_STARTED else "broker-entry-1",
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("100000"),
        filled_quantity=Decimal("0"),
        state=state,
        version=4,
        updated_at=NOW,
    )


class _RecoveryStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, record: BybitEntryRecoveryRecord) -> None:
        self.record = record

    def load(self, *, entry_order_link_id: str) -> BybitEntryRecoveryRecord:
        assert entry_order_link_id == ENTRY_LINK
        return self.record


@dataclass
class _LeaseRecord:
    owner_token: str = "f" * 64
    fencing_token: int = 8


class _Lease:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_stale_takeover_allowed = False

    def __init__(self, *, stale_busy: bool = False, recovery_allowed: bool = True) -> None:
        self.stale_busy = stale_busy
        self.recovery_allowed = recovery_allowed
        self.recovered = False
        self.released = False
        self.acquire_calls = 0
        self.proofs = []

    def acquire(self) -> _LeaseRecord:
        self.acquire_calls += 1
        if self.stale_busy and not self.recovered:
            raise FileExistsError("busy")
        return _LeaseRecord()

    def inspect(self):
        return SimpleNamespace(fencing_token=7)

    def recover_expired(self, *, expected_fencing_token, broker_reconciliation, operator_reason):
        assert expected_fencing_token == 7
        assert operator_reason
        self.proofs.append(broker_reconciliation)
        if not self.recovery_allowed:
            raise RuntimeError("old lease is not expired")
        self.recovered = True

    def release(self, *, owner_token: str) -> None:
        assert owner_token == "f" * 64
        self.released = True


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, checkpoint: BybitDemoExcursionCheckpoint | None = None) -> None:
        self.checkpoint = checkpoint
        self.initialize_calls = 0

    def load(self) -> BybitDemoExcursionCheckpoint:
        if self.checkpoint is None:
            raise FileNotFoundError
        return self.checkpoint

    def initialize(self, *, entry_order_link_id, state) -> BybitDemoExcursionCheckpoint:
        self.initialize_calls += 1
        assert entry_order_link_id == ENTRY_LINK
        assert self.checkpoint is None
        self.checkpoint = BybitDemoExcursionCheckpoint(
            entry_order_link_id=entry_order_link_id,
            state=state,
            revision="a" * 64,
        )
        return self.checkpoint


class _EntryOms:
    live_mainnet_order_routing_allowed = False

    def __init__(self, record: OrderRecord) -> None:
        self.record = record

    def get(self, intent_id: str) -> OrderRecord | None:
        assert intent_id == INTENT_ID
        return self.record

    def mark_lifecycle_reconciliation_required(
        self,
        intent_id,
        *,
        broker_order_id,
        broker_status,
        cumulative_executed_quantity,
        occurred_at,
    ) -> OrderRecord:
        assert intent_id == INTENT_ID
        assert broker_status in {"Filled", "Cancelled"}
        assert cumulative_executed_quantity > 0
        self.record = replace(
            self.record,
            state=OrderState.RECONCILING,
            broker_order_id=broker_order_id,
            updated_at=occurred_at,
        )
        return self.record

    def transition(
        self,
        intent_id,
        target,
        *,
        event_id,
        occurred_at,
        broker_order_id=None,
        payload=None,
    ) -> OrderRecord:
        assert intent_id == INTENT_ID
        assert event_id
        assert payload is not None
        self.record = replace(
            self.record,
            state=target,
            broker_order_id=self.record.broker_order_id if broker_order_id is None else broker_order_id,
            updated_at=occurred_at,
        )
        return self.record

    def apply_cumulative_fill(
        self,
        intent_id,
        *,
        event_id,
        cumulative_filled,
        occurred_at,
        broker_order_id=None,
    ) -> OrderRecord:
        assert intent_id == INTENT_ID
        assert event_id
        target = (
            OrderState.FILLED
            if cumulative_filled == self.record.quantity
            else OrderState.PARTIALLY_FILLED
        )
        self.record = replace(
            self.record,
            state=target,
            filled_quantity=cumulative_filled,
            broker_order_id=self.record.broker_order_id if broker_order_id is None else broker_order_id,
            updated_at=occurred_at,
        )
        return self.record


class _Client:
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True


def _safety_result(plan, status: BybitExecutedEntryRecoveryStatus):
    closed = status is BybitExecutedEntryRecoveryStatus.FLATTENED
    return BybitExecutedEntryRecoveryResult(
        status=status,
        reasons=(),
        plan=plan,
        protection_ack=None,
        flatten_ack=(
            BybitDemoOrderAck("close-1", "ASTRA-DEMO-C-CONVERGE-1", True)
            if closed
            else None
        ),
        broker_position_closed=closed,
    )


def test_stale_lease_recovery_protects_checkpoints_and_converges_oms(monkeypatch) -> None:
    calls = []

    def _execute(plan, *, client):
        assert isinstance(client, _Client)
        calls.append("safety")
        return _safety_result(plan, BybitExecutedEntryRecoveryStatus.PROTECTED)

    monkeypatch.setattr(convergence_module, "execute_bybit_executed_entry_recovery", _execute)
    lease = _Lease(stale_busy=True, recovery_allowed=True)
    store = _ExcursionStore()
    oms = _EntryOms(_oms_record())

    result = converge_bybit_executed_entry_recovery(
        oms.record,
        order_truth=_truth(),
        positions=(_position(),),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=lease,
        excursion_store=store,
        entry_oms=oms,
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.ACTIVE_MANAGEMENT_READY
    assert result.stale_lease_recovered is True
    assert result.runtime_lease_acquired is True
    assert result.runtime_lease_released is True
    assert result.next_entry_allowed is False
    assert calls == ["safety"]
    assert store.initialize_calls == 1
    assert result.checkpoint is store.checkpoint
    assert oms.record.state is OrderState.FILLED
    assert oms.record.filled_quantity == Decimal("0.01")
    assert lease.acquire_calls == 2
    assert lease.released is True
    assert lease.proofs[0].broker_truth_complete is True
    assert lease.proofs[0].next_entry_allowed is False


def test_unexpired_foreign_lease_blocks_before_safety_mutation(monkeypatch) -> None:
    calls = []

    def _execute(*_args, **_kwargs):
        calls.append("unsafe")
        raise AssertionError("safety mutation must not run without recovery fence")

    monkeypatch.setattr(convergence_module, "execute_bybit_executed_entry_recovery", _execute)
    lease = _Lease(stale_busy=True, recovery_allowed=False)
    store = _ExcursionStore()
    oms = _EntryOms(_oms_record())

    result = converge_bybit_executed_entry_recovery(
        oms.record,
        order_truth=_truth(),
        positions=(_position(),),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=lease,
        excursion_store=store,
        entry_oms=oms,
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.BLOCKED
    assert result.reasons == ("RECOVERY_RUNTIME_LEASE_UNAVAILABLE:RuntimeError",)
    assert result.runtime_lease_acquired is False
    assert calls == []
    assert store.initialize_calls == 0
    assert oms.record.state is OrderState.SUBMIT_STARTED


def test_flattened_recovery_still_checkpoints_for_terminal_accounting(monkeypatch) -> None:
    monkeypatch.setattr(
        convergence_module,
        "execute_bybit_executed_entry_recovery",
        lambda plan, *, client: _safety_result(
            plan,
            BybitExecutedEntryRecoveryStatus.FLATTENED,
        ),
    )
    store = _ExcursionStore()
    oms = _EntryOms(_oms_record(state=OrderState.ACKNOWLEDGED))

    result = converge_bybit_executed_entry_recovery(
        oms.record,
        order_truth=_truth(),
        positions=(_position(),),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=_Lease(),
        excursion_store=store,
        entry_oms=oms,
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.TERMINAL_HANDOFF_REQUIRED
    assert result.checkpoint is not None
    assert result.checkpoint.state.initial_quantity == Decimal("0.01")
    assert oms.record.state is OrderState.FILLED
    assert result.next_entry_allowed is False


def test_partial_cancelled_entry_converges_oms_cancelled_but_keeps_managed_position(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        convergence_module,
        "execute_bybit_executed_entry_recovery",
        lambda plan, *, client: _safety_result(
            plan,
            BybitExecutedEntryRecoveryStatus.PROTECTED,
        ),
    )
    oms = _EntryOms(_oms_record(state=OrderState.ACKNOWLEDGED))

    result = converge_bybit_executed_entry_recovery(
        oms.record,
        order_truth=_truth(status="Cancelled", executed="0.005"),
        positions=(_position(size="0.005"),),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=_Lease(),
        excursion_store=_ExcursionStore(),
        entry_oms=oms,
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.ACTIVE_MANAGEMENT_READY
    assert oms.record.state is OrderState.CANCELLED
    assert oms.record.filled_quantity == Decimal("0.005")
    assert result.checkpoint is not None
    assert result.checkpoint.state.initial_quantity == Decimal("0.005")


def test_existing_checkpoint_and_flat_broker_skips_safety_and_requires_terminal_handoff(
    monkeypatch,
) -> None:
    existing_state = convergence_module.build_recovered_entry_excursion_state(
        _safety_result(
            convergence_module.plan_bybit_executed_entry_recovery(
                _recovery_record(),
                order_truth=_truth(),
                positions=(_position(),),
            ),
            BybitExecutedEntryRecoveryStatus.PROTECTED,
        )
    )
    checkpoint = BybitDemoExcursionCheckpoint(
        entry_order_link_id=ENTRY_LINK,
        state=existing_state,
        revision="b" * 64,
    )
    safety_calls = []
    monkeypatch.setattr(
        convergence_module,
        "execute_bybit_executed_entry_recovery",
        lambda *_args, **_kwargs: safety_calls.append("unexpected"),
    )
    oms = _EntryOms(_oms_record(state=OrderState.ACKNOWLEDGED))

    result = converge_bybit_executed_entry_recovery(
        oms.record,
        order_truth=_truth(),
        positions=(),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=_Lease(),
        excursion_store=_ExcursionStore(checkpoint),
        entry_oms=oms,
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.TERMINAL_HANDOFF_REQUIRED
    assert result.checkpoint == checkpoint
    assert safety_calls == []
    assert oms.record.state is OrderState.FILLED
    assert result.next_entry_allowed is False


def test_unresolved_safety_never_creates_checkpoint(monkeypatch) -> None:
    def _unresolved(plan, *, client):
        return BybitExecutedEntryRecoveryResult(
            status=BybitExecutedEntryRecoveryStatus.UNRESOLVED,
            reasons=("RESIDUAL_POSITION_UNKNOWN",),
            plan=plan,
            protection_ack=None,
            flatten_ack=None,
            broker_position_closed=None,
        )

    monkeypatch.setattr(convergence_module, "execute_bybit_executed_entry_recovery", _unresolved)
    store = _ExcursionStore()

    result = converge_bybit_executed_entry_recovery(
        _oms_record(),
        order_truth=_truth(),
        positions=(_position(),),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=_Lease(),
        excursion_store=store,
        entry_oms=_EntryOms(_oms_record()),
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.BLOCKED
    assert result.reasons == ("RECOVERY_SAFETY_STATE_UNRESOLVED",)
    assert store.initialize_calls == 0
    assert result.next_entry_allowed is False


def test_open_partially_filled_order_is_rejected_before_recovery_ownership() -> None:
    result = converge_bybit_executed_entry_recovery(
        _oms_record(),
        order_truth=_truth(status="PartiallyFilled", executed="0.005"),
        positions=(_position(size="0.005"),),
        recovery_store=_RecoveryStore(_recovery_record()),
        runtime_lease=_Lease(),
        excursion_store=_ExcursionStore(),
        entry_oms=_EntryOms(_oms_record()),
        client=_Client(),
        occurred_at=NOW,
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.BLOCKED
    assert result.reasons == ("RECOVERY_PLAN_REJECTED:ValueError",)
