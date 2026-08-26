from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution import bybit_demo_operational_reconciliation as reconciliation_module
from app.execution.bybit_demo_operational_entry import BybitDemoOperationalProtectionStatus
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.execution.bybit_entry_restart_recovery import BybitExecutedEntryRecoveryStatus
from app.oms.store import OrderState


_NOW = datetime(2026, 8, 26, 20, 6, tzinfo=UTC)


class _AuthorizationStore:
    immutable_records = True
    live_mainnet_order_routing_allowed = False

    def __init__(self, record=None) -> None:
        self.record = record
        self.load_count = 0

    def load(self, *, entry_order_link_id: str):
        self.load_count += 1
        if self.record is None:
            raise FileNotFoundError(entry_order_link_id)
        return self.record


class _EntryOms:
    live_mainnet_order_routing_allowed = False
    automatic_resubmit_after_submit_started_allowed = False

    def __init__(self, record=None) -> None:
        self.record = record
        self.get_count = 0

    def get(self, intent_id: str):
        assert intent_id
        self.get_count += 1
        return self.record


class _RecoveryStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, record=None) -> None:
        self.record = record
        self.load_count = 0

    def load(self, *, entry_order_link_id: str):
        self.load_count += 1
        if self.record is None:
            raise FileNotFoundError(entry_order_link_id)
        return self.record


class _BrokerClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(self) -> None:
        self.position_reads = 0
        self.get_calls = 0

    def _signed_get(self, path, params):
        self.get_calls += 1
        raise AssertionError((path, params))

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        self.position_reads += 1
        return ()


def _approval() -> BybitDemoOperatorApproval:
    return BybitDemoOperatorApproval(
        source_snapshot_id="a" * 64,
        source_evidence_rank=1,
        source_market_rank=2,
        symbol="BTCUSDT",
        side="LONG",
        decision_time="2026-08-26T20:00:00+00:00",
        signal_available_at="2026-08-26T20:05:00+00:00",
        signal_quality_score=Decimal("1.25"),
        source_planned_notional_usdt=Decimal("100"),
        source_risk_budget_usdt=Decimal("10"),
        source_modeled_round_trip_cost_usdt=Decimal("0.25"),
        maximum_entry_quantity=Decimal("0.01"),
        approved_at="2026-08-26T20:05:30+00:00",
        expires_at=(_NOW + timedelta(seconds=60)).isoformat(),
    )


def _authorization_record(approval: BybitDemoOperatorApproval):
    authorization = SimpleNamespace(
        approval_id=approval.approval_id,
        expected_entry_order_link_id=approval.expected_entry_order_link_id,
        source_snapshot_id=approval.source_snapshot_id,
        source_evidence_rank=approval.source_evidence_rank,
        source_market_rank=approval.source_market_rank,
    )
    return SimpleNamespace(
        authorization=authorization,
        live_mainnet_order_routing_allowed=False,
    )


def _oms_record(approval: BybitDemoOperatorApproval, *, state: OrderState):
    return SimpleNamespace(
        client_order_id=approval.expected_entry_order_link_id,
        symbol=approval.symbol,
        state=state,
        filled_quantity=Decimal("0"),
    )


def _recovery_record(approval: BybitDemoOperatorApproval):
    class _Envelope:
        entry_order_link_id = approval.expected_entry_order_link_id
        order_side = "Buy"
        approved_order_quantity = Decimal("0.01")
        trade_plan = SimpleNamespace(symbol=approval.symbol)

        @staticmethod
        def validate() -> None:
            return None

    return SimpleNamespace(
        envelope=_Envelope(),
        record_sha256="f" * 64,
        live_mainnet_order_routing_allowed=False,
    )


def test_no_authorization_proves_no_entry_attempt_reached_network_boundary() -> None:
    approval = _approval()
    authorization_store = _AuthorizationStore()
    entry_oms = _EntryOms()
    broker = _BrokerClient()

    result = reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
        approval,
        None,
        authorization_store=authorization_store,
        entry_oms=entry_oms,
        recovery_store=_RecoveryStore(),
        broker_client=broker,
    )

    assert result.status is BybitDemoOperationalProtectionStatus.NO_ENTRY_AUTHORIZATION
    assert result.completed
    assert result.entry_execution_confirmed is False
    assert not result.safety_mutation_performed
    assert authorization_store.load_count == 1
    assert entry_oms.get_count == 0
    assert broker.get_calls == 0


def test_burned_authorization_without_oms_claim_proves_no_execution() -> None:
    approval = _approval()
    authorization_store = _AuthorizationStore(_authorization_record(approval))
    entry_oms = _EntryOms(record=None)

    result = reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
        approval,
        None,
        authorization_store=authorization_store,
        entry_oms=entry_oms,
        recovery_store=_RecoveryStore(),
        broker_client=_BrokerClient(),
    )

    assert result.status is BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED
    assert result.completed
    assert result.entry_execution_confirmed is False
    assert entry_oms.get_count == 1


def test_pre_submit_oms_state_does_not_issue_broker_read_or_retry() -> None:
    approval = _approval()
    broker = _BrokerClient()
    result = reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
        approval,
        None,
        authorization_store=_AuthorizationStore(_authorization_record(approval)),
        entry_oms=_EntryOms(_oms_record(approval, state=OrderState.OUTBOXED)),
        recovery_store=_RecoveryStore(),
        broker_client=broker,
    )
    assert result.status is BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED
    assert result.entry_execution_confirmed is False
    assert broker.get_calls == 0


def test_submit_started_without_broker_truth_remains_unresolved(monkeypatch) -> None:
    approval = _approval()
    monkeypatch.setattr(
        reconciliation_module,
        "lookup_bybit_order_by_link_id",
        lambda *args, **kwargs: None,
    )
    result = reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
        approval,
        None,
        authorization_store=_AuthorizationStore(_authorization_record(approval)),
        entry_oms=_EntryOms(_oms_record(approval, state=OrderState.SUBMIT_STARTED)),
        recovery_store=_RecoveryStore(_recovery_record(approval)),
        broker_client=_BrokerClient(),
    )
    assert result.status is BybitDemoOperationalProtectionStatus.UNRESOLVED
    assert not result.completed
    assert result.entry_execution_confirmed is None
    assert not result.second_entry_submit_performed


def test_confirmed_execution_uses_existing_safety_recovery_only(monkeypatch) -> None:
    approval = _approval()
    broker = _BrokerClient()
    calls = {"lookup": 0, "plan": 0, "execute": 0}

    def lookup(*args, **kwargs):
        calls["lookup"] += 1
        assert kwargs["order_link_id"] == approval.expected_entry_order_link_id
        return SimpleNamespace(
            cumulative_executed_quantity=Decimal("0.01"),
            status="Filled",
        )

    def plan(recovery, *, order_truth, positions):
        calls["plan"] += 1
        assert recovery.record_sha256 == "f" * 64
        assert order_truth.status == "Filled"
        assert positions == ()
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    def execute(plan_value, *, client):
        calls["execute"] += 1
        assert plan_value.live_mainnet_order_routing_allowed is False
        assert client is broker
        return SimpleNamespace(status=BybitExecutedEntryRecoveryStatus.PROTECTED)

    monkeypatch.setattr(reconciliation_module, "lookup_bybit_order_by_link_id", lookup)
    monkeypatch.setattr(
        reconciliation_module,
        "plan_bybit_executed_entry_recovery",
        plan,
    )
    monkeypatch.setattr(
        reconciliation_module,
        "execute_bybit_executed_entry_recovery",
        execute,
    )

    result = reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
        approval,
        None,
        authorization_store=_AuthorizationStore(_authorization_record(approval)),
        entry_oms=_EntryOms(_oms_record(approval, state=OrderState.UNCERTAIN)),
        recovery_store=_RecoveryStore(_recovery_record(approval)),
        broker_client=broker,
    )

    assert calls == {"lookup": 1, "plan": 1, "execute": 1}
    assert broker.position_reads == 1
    assert result.status is BybitDemoOperationalProtectionStatus.RECOVERED_PROTECTED
    assert result.completed
    assert result.entry_execution_confirmed is True
    assert result.safety_mutation_performed
    assert not result.second_entry_submit_performed


def test_canonical_persisted_provenance_is_already_reconciled() -> None:
    approval = _approval()
    authorization_store = _AuthorizationStore()
    runtime = SimpleNamespace(
        entry_provenance_persisted=True,
        entry_provenance_receipt=SimpleNamespace(
            entry_order_link_id=approval.expected_entry_order_link_id,
        ),
        same_invocation_additional_entry_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )
    runtime_result = SimpleNamespace(
        authorization_persisted=True,
        runtime_result=runtime,
    )

    result = reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
        approval,
        runtime_result,
        authorization_store=authorization_store,
        entry_oms=_EntryOms(),
        recovery_store=_RecoveryStore(),
        broker_client=_BrokerClient(),
    )

    assert result.status is BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED
    assert result.completed
    assert authorization_store.load_count == 0


def test_mismatched_authorization_lineage_fails_closed() -> None:
    approval = _approval()
    record = _authorization_record(approval)
    record.authorization.approval_id = "0" * 64
    with pytest.raises(ValueError, match="authorization approval id mismatch"):
        reconciliation_module.reconcile_protected_bybit_demo_entry_attempt(
            approval,
            None,
            authorization_store=_AuthorizationStore(record),
            entry_oms=_EntryOms(),
            recovery_store=_RecoveryStore(),
            broker_client=_BrokerClient(),
        )
