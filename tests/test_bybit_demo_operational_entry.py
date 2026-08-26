from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo import BybitDemoOrderClient
from app.execution.bybit_demo_connected_preflight import BybitDemoConnectedPreflightStatus
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlDecision,
    BybitDemoControlMode,
)
from app.execution.bybit_demo_operational_entry import (
    BybitDemoOperationalEntryStatus,
    BybitDemoOperationalProtectionReconciliation,
    BybitDemoOperationalProtectionStatus,
    run_protected_bybit_demo_operational_entry,
)
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.execution.bybit_demo_trading_runtime import BybitDemoTradingRuntimeStatus


_NOW = datetime(2026, 8, 26, 20, 6, tzinfo=UTC)
_ARM_ID = "b" * 64


class _MissingStore:
    immutable_records = True
    live_mainnet_order_routing_allowed = False

    def load(self, *, entry_order_link_id: str):
        raise FileNotFoundError(entry_order_link_id)


class _ExistingStore(_MissingStore):
    def load(self, *, entry_order_link_id: str):
        return SimpleNamespace(
            entry_order_link_id=entry_order_link_id,
            live_mainnet_order_routing_allowed=False,
        )


class _RecoveryStore(_MissingStore):
    order_writes_supported = False


class _EntryOms:
    live_mainnet_order_routing_allowed = False
    automatic_resubmit_after_submit_started_allowed = False


class _Client(BybitDemoOrderClient):
    entry_recovery_required = True

    def __init__(self, *, mainnet: bool = False) -> None:
        self.entry_oms = _EntryOms()
        self.entry_recovery_store = _RecoveryStore()
        self._test_mainnet = mainnet

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return self._test_mainnet


class _ControlPlane:
    fixed_egress_required = True
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True

    def __init__(self, decisions: list[BybitDemoControlDecision] | None = None) -> None:
        self.decisions = decisions or [_armed_decision()]
        self.read_count = 0

    def read_decision(self, *, now: datetime) -> BybitDemoControlDecision:
        del now
        index = min(self.read_count, len(self.decisions) - 1)
        self.read_count += 1
        return self.decisions[index]


def _approval(*, expires_at: datetime | None = None) -> BybitDemoOperatorApproval:
    expiry = expires_at or (_NOW + timedelta(seconds=60))
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
        expires_at=expiry.isoformat(),
    )


def _ready_preflight():
    return SimpleNamespace(
        status=BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL,
        reasons=(),
        read_only_api_key_verified=True,
        api_key_ip_binding_present=True,
        order_writes_supported=False,
        live_mainnet_order_routing_allowed=False,
    )


def _blocked_preflight():
    return SimpleNamespace(
        status=BybitDemoConnectedPreflightStatus.BLOCKED,
        reasons=("FIXED_EGRESS_NOT_VERIFIED",),
        read_only_api_key_verified=False,
        api_key_ip_binding_present=False,
        order_writes_supported=False,
        live_mainnet_order_routing_allowed=False,
    )


def _armed_decision(
    *,
    event_id: str = _ARM_ID,
    armed_until: datetime | None = None,
) -> BybitDemoControlDecision:
    return BybitDemoControlDecision(
        mode=BybitDemoControlMode.ARMED_NEW_ENTRIES,
        reasons=(),
        new_entry_allowed=True,
        latest_event_id=event_id,
        latest_event_kind="ARM_NEW_ENTRIES",
        armed_until=armed_until or (_NOW + timedelta(minutes=2)),
    )


def _runtime_result(approval: BybitDemoOperatorApproval):
    authorization_receipt = SimpleNamespace(
        approval_id=approval.approval_id,
        entry_order_link_id=approval.expected_entry_order_link_id,
        record_sha256="c" * 64,
    )
    provenance_receipt = SimpleNamespace(
        entry_order_link_id=approval.expected_entry_order_link_id,
        record_sha256="d" * 64,
    )
    runtime = SimpleNamespace(
        status=BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED,
        live_mainnet_order_routing_allowed=False,
        demo_only=True,
        same_invocation_additional_entry_allowed=False,
        entry_provenance=SimpleNamespace(
            entry_order_link_id=approval.expected_entry_order_link_id,
        ),
        entry_provenance_receipt=provenance_receipt,
        entry_provenance_persisted=True,
    )
    return SimpleNamespace(
        runtime_result=runtime,
        authorization=SimpleNamespace(
            approval_id=approval.approval_id,
            expected_entry_order_link_id=approval.expected_entry_order_link_id,
        ),
        authorization_receipt=authorization_receipt,
        authorization_persisted=True,
        demo_only=True,
        live_mainnet_order_routing_allowed=False,
    )


def _runtime_kwargs(
    *,
    authorization_store=None,
    provenance_store=None,
    client=None,
):
    return {
        "client": client or _Client(),
        "approval_authorization_store": authorization_store or _MissingStore(),
        "entry_provenance_store": provenance_store or _MissingStore(),
        "now_ms": int(_NOW.timestamp() * 1000),
    }


def _canonical_reconciliation(
    approval: BybitDemoOperatorApproval,
    runtime_result,
) -> BybitDemoOperationalProtectionReconciliation:
    assert approval.symbol == "BTCUSDT"
    assert runtime_result is not None
    return BybitDemoOperationalProtectionReconciliation(
        status=BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED,
        completed=True,
        entry_execution_confirmed=True,
        safety_mutation_performed=False,
    )


def test_operational_entry_delegates_once_and_emits_allowlisted_evidence() -> None:
    approval = _approval()
    control = _ControlPlane()
    calls = {"runtime": 0, "reconciliation": 0}

    def runtime_runner(*args, **kwargs):
        calls["runtime"] += 1
        assert args[0] is approval
        kwargs["new_entry_control_plane"].read_decision(
            now=kwargs["control_now_provider"]()
        )
        return _runtime_result(approval)

    def reconciler(*args):
        calls["reconciliation"] += 1
        return _canonical_reconciliation(*args)

    evidence = run_protected_bybit_demo_operational_entry(
        approval,
        {},
        {},
        fixed_egress_preflight=_ready_preflight(),
        new_entry_control_plane=control,
        post_attempt_reconciler=reconciler,
        now=_NOW,
        control_now_provider=lambda: _NOW,
        runtime_runner=runtime_runner,
        **_runtime_kwargs(),
    )

    assert calls == {"runtime": 1, "reconciliation": 1}
    assert control.read_count == 2
    assert evidence.status is BybitDemoOperationalEntryStatus.ENTRY_CYCLE_COMPLETE
    assert evidence.authorization_persisted
    assert evidence.entry_provenance_persisted
    assert not evidence.same_invocation_additional_entry_allowed
    payload = evidence.to_payload()
    assert payload["pinned_control_event_id"] == _ARM_ID
    assert payload["automatic_arm_allowed"] is False
    assert payload["ranked_fallback_allowed"] is False
    assert payload["live_mainnet_order_routing_allowed"] is False
    forbidden = {
        "api_key",
        "api_secret",
        "dsn",
        "request_payload",
        "response_payload",
        "broker_order_id",
        "realized_pnl",
    }
    assert forbidden.isdisjoint(payload)


def test_expired_approval_blocks_before_runtime_or_reconciliation() -> None:
    approval = _approval(expires_at=_NOW - timedelta(seconds=1))
    calls = {"runtime": 0, "reconciliation": 0}

    def runtime_runner(*args, **kwargs):
        calls["runtime"] += 1
        raise AssertionError((args, kwargs))

    def reconciler(*args):
        calls["reconciliation"] += 1
        raise AssertionError(args)

    with pytest.raises(ValueError, match="approval is not valid"):
        run_protected_bybit_demo_operational_entry(
            approval,
            {},
            {},
            fixed_egress_preflight=_ready_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=reconciler,
            now=_NOW,
            runtime_runner=runtime_runner,
            **_runtime_kwargs(),
        )
    assert calls == {"runtime": 0, "reconciliation": 0}


def test_unverified_fixed_egress_blocks_before_runtime() -> None:
    with pytest.raises(ValueError, match="fixed-egress connected preflight readiness"):
        run_protected_bybit_demo_operational_entry(
            _approval(),
            {},
            {},
            fixed_egress_preflight=_blocked_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=_canonical_reconciliation,
            now=_NOW,
            runtime_runner=lambda *args, **kwargs: pytest.fail("runtime must not run"),
            **_runtime_kwargs(),
        )


def test_existing_authorization_blocks_repeat_entry_before_runtime() -> None:
    with pytest.raises(RuntimeError, match="ENTRY_AUTHORIZATION_ALREADY_EXISTS"):
        run_protected_bybit_demo_operational_entry(
            _approval(),
            {},
            {},
            fixed_egress_preflight=_ready_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=_canonical_reconciliation,
            now=_NOW,
            runtime_runner=lambda *args, **kwargs: pytest.fail("runtime must not run"),
            **_runtime_kwargs(authorization_store=_ExistingStore()),
        )


def test_existing_provenance_blocks_repeat_entry_before_runtime() -> None:
    with pytest.raises(RuntimeError, match="ENTRY_PROVENANCE_ALREADY_EXISTS"):
        run_protected_bybit_demo_operational_entry(
            _approval(),
            {},
            {},
            fixed_egress_preflight=_ready_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=_canonical_reconciliation,
            now=_NOW,
            runtime_runner=lambda *args, **kwargs: pytest.fail("runtime must not run"),
            **_runtime_kwargs(provenance_store=_ExistingStore()),
        )


def test_mainnet_capable_dependency_blocks_before_runtime() -> None:
    with pytest.raises(ValueError, match="mainnet-capable order client"):
        run_protected_bybit_demo_operational_entry(
            _approval(),
            {},
            {},
            fixed_egress_preflight=_ready_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=_canonical_reconciliation,
            now=_NOW,
            runtime_runner=lambda *args, **kwargs: pytest.fail("runtime must not run"),
            **_runtime_kwargs(client=_Client(mainnet=True)),
        )


def test_non_demo_client_family_blocks_before_runtime() -> None:
    unsafe = SimpleNamespace(
        environment="BYBIT_DEMO",
        live_mainnet_order_routing_allowed=False,
        entry_recovery_required=True,
        entry_oms=_EntryOms(),
        entry_recovery_store=_RecoveryStore(),
    )
    with pytest.raises(ValueError, match="concrete Demo-only order client family"):
        run_protected_bybit_demo_operational_entry(
            _approval(),
            {},
            {},
            fixed_egress_preflight=_ready_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=_canonical_reconciliation,
            now=_NOW,
            runtime_runner=lambda *args, **kwargs: pytest.fail("runtime must not run"),
            **_runtime_kwargs(client=unsafe),
        )


def test_automatic_oms_resubmit_capability_blocks_before_runtime() -> None:
    client = _Client()
    client.entry_oms.automatic_resubmit_after_submit_started_allowed = True
    with pytest.raises(ValueError, match="forbids automatic entry resubmit"):
        run_protected_bybit_demo_operational_entry(
            _approval(),
            {},
            {},
            fixed_egress_preflight=_ready_preflight(),
            new_entry_control_plane=_ControlPlane(),
            post_attempt_reconciler=_canonical_reconciliation,
            now=_NOW,
            runtime_runner=lambda *args, **kwargs: pytest.fail("runtime must not run"),
            **_runtime_kwargs(client=client),
        )


def test_pinned_arm_change_blocks_runtime_but_still_reconciles() -> None:
    approval = _approval()
    changed = _armed_decision(event_id="e" * 64)
    control = _ControlPlane([_armed_decision(), changed])
    reconciliation_calls = 0

    def runtime_runner(*args, **kwargs):
        kwargs["new_entry_control_plane"].read_decision(
            now=kwargs["control_now_provider"]()
        )
        pytest.fail("pinned ARM change must have raised")

    def reconciler(_approval, runtime_result):
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        assert runtime_result is None
        return BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.NO_ENTRY_AUTHORIZATION,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
        )

    evidence = run_protected_bybit_demo_operational_entry(
        approval,
        {},
        {},
        fixed_egress_preflight=_ready_preflight(),
        new_entry_control_plane=control,
        post_attempt_reconciler=reconciler,
        now=_NOW,
        control_now_provider=lambda: _NOW,
        runtime_runner=runtime_runner,
        **_runtime_kwargs(),
    )
    assert reconciliation_calls == 1
    assert evidence.status is BybitDemoOperationalEntryStatus.ENTRY_BLOCKED
    assert evidence.runtime_error_type == "RuntimeError"


def test_runtime_failure_still_reconciles_and_does_not_leak_error_text() -> None:
    calls = {"runtime": 0, "reconciliation": 0}

    def runtime_runner(*args, **kwargs):
        calls["runtime"] += 1
        kwargs["new_entry_control_plane"].read_decision(
            now=kwargs["control_now_provider"]()
        )
        raise RuntimeError("SECRET SHOULD NEVER APPEAR IN EVIDENCE")

    def reconciler(_approval, runtime_result):
        calls["reconciliation"] += 1
        assert runtime_result is None
        return BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.UNRESOLVED,
            completed=False,
            entry_execution_confirmed=None,
            safety_mutation_performed=False,
        )

    evidence = run_protected_bybit_demo_operational_entry(
        _approval(),
        {},
        {},
        fixed_egress_preflight=_ready_preflight(),
        new_entry_control_plane=_ControlPlane(),
        post_attempt_reconciler=reconciler,
        now=_NOW,
        control_now_provider=lambda: _NOW,
        runtime_runner=runtime_runner,
        **_runtime_kwargs(),
    )
    assert calls == {"runtime": 1, "reconciliation": 1}
    assert evidence.status is BybitDemoOperationalEntryStatus.ENTRY_BLOCKED
    assert evidence.runtime_error_type == "RuntimeError"
    assert "SECRET SHOULD NEVER APPEAR" not in json.dumps(evidence.to_payload())


def test_reconciler_cannot_claim_a_second_entry_submit() -> None:
    approval = _approval()

    def runtime_runner(*args, **kwargs):
        kwargs["new_entry_control_plane"].read_decision(
            now=kwargs["control_now_provider"]()
        )
        return _runtime_result(approval)

    def unsafe_reconciler(*_args):
        return BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=False,
            second_entry_submit_performed=True,
        )

    evidence = run_protected_bybit_demo_operational_entry(
        approval,
        {},
        {},
        fixed_egress_preflight=_ready_preflight(),
        new_entry_control_plane=_ControlPlane(),
        post_attempt_reconciler=unsafe_reconciler,
        now=_NOW,
        control_now_provider=lambda: _NOW,
        runtime_runner=runtime_runner,
        **_runtime_kwargs(),
    )
    assert evidence.status is BybitDemoOperationalEntryStatus.ENTRY_BLOCKED
    assert (
        evidence.protection_reconciliation_status
        is BybitDemoOperationalProtectionStatus.UNRESOLVED
    )
    assert not evidence.protection_reconciliation_completed
