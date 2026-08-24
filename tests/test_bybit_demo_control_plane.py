from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoOrderRequest
from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightResult,
    BybitDemoConnectedPreflightStatus,
)
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlDecision,
    BybitDemoControlMode,
    ControlPlaneGuardedBybitDemoClient,
    PostgresBybitDemoControlPlane,
)

_NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


def _preflight(
    status: BybitDemoConnectedPreflightStatus = (
        BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
    ),
) -> BybitDemoConnectedPreflightResult:
    return BybitDemoConnectedPreflightResult(
        status=status,
        reasons=(),
        margin_mode="REGULAR_MARGIN",
        unified_margin_status=6,
        positive_equity=True,
        positive_available_balance=True,
        usdt_wallet_visible=True,
        open_position_count=(
            1
            if status is BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
            else 0
        ),
        open_position_symbols=(
            ("BTCUSDT",)
            if status is BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
            else ()
        ),
        open_order_count=0,
        open_order_symbols=(),
        active_checkpoint_present=(
            status is BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
        ),
        active_checkpoint_symbol=(
            "BTCUSDT"
            if status is BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
            else None
        ),
        runtime_lease_present=False,
        required_relations_present=True,
        append_only_triggers_present=True,
        approval_record_count=0,
        provenance_record_count=0,
        terminal_record_count=0,
        read_only_api_key_verified=True,
        api_key_ip_binding_present=True,
    )


def _decision(*, armed: bool) -> BybitDemoControlDecision:
    return BybitDemoControlDecision(
        mode=(
            BybitDemoControlMode.ARMED_NEW_ENTRIES
            if armed
            else BybitDemoControlMode.HALTED
        ),
        reasons=() if armed else ("DEMO_CONTROL_OPERATOR_HALT",),
        new_entry_allowed=armed,
        latest_event_id="a" * 64,
        latest_event_kind="ARM_NEW_ENTRIES" if armed else "HALT_NEW_ENTRIES",
        armed_until=_NOW + timedelta(minutes=2) if armed else None,
    )


class _Control:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True

    def __init__(self, decisions: list[BybitDemoControlDecision]) -> None:
        self.decisions = decisions
        self.read_calls = 0

    def read_decision(self, *, now: datetime) -> BybitDemoControlDecision:
        assert now.tzinfo is not None
        self.read_calls += 1
        if not self.decisions:
            raise AssertionError("unexpected control-plane read")
        return self.decisions.pop(0)


class _RawDemoClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(self) -> None:
        self.orders: list[BybitDemoOrderRequest] = []

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        self.orders.append(request)
        return BybitDemoOrderAck(
            order_id="OID-1",
            order_link_id=request.order_link_id,
            accepted=True,
        )


def _order(*, reduce_only: bool) -> BybitDemoOrderRequest:
    return BybitDemoOrderRequest(
        symbol="BTCUSDT",
        side="Sell" if reduce_only else "Buy",
        quantity=Decimal("0.01"),
        order_link_id=(
            "ASTRA-DEMO-C-CONTROL" if reduce_only else "ASTRA-DEMO-E-CONTROL"
        ),
        reduce_only=reduce_only,
    )


def test_arm_rejects_existing_trade_management_preflight_before_database_access() -> None:
    plane = PostgresBybitDemoControlPlane("postgresql://not-used")

    with pytest.raises(ValueError, match="clean connected preflight"):
        plane.arm_new_entries(
            _preflight(
                BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
            ),
            operator_id="operator",
            reason="should not arm over an existing trade",
            now=_NOW,
            preflight_observed_at=_NOW,
        )


def test_arm_rejects_stale_preflight_before_database_access() -> None:
    plane = PostgresBybitDemoControlPlane("postgresql://not-used")

    with pytest.raises(ValueError, match="too old"):
        plane.arm_new_entries(
            _preflight(),
            operator_id="operator",
            reason="stale preflight must not arm",
            now=_NOW,
            preflight_observed_at=_NOW - timedelta(seconds=31),
        )


def test_control_guard_blocks_non_reduce_only_entry_when_halted() -> None:
    raw = _RawDemoClient()
    control = _Control([_decision(armed=False)])
    guarded = ControlPlaneGuardedBybitDemoClient(
        raw,
        control,  # type: ignore[arg-type]
        now_provider=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="halted by control plane"):
        guarded.place_market_order(_order(reduce_only=False))

    assert control.read_calls == 1
    assert raw.orders == []


def test_control_guard_allows_armed_non_reduce_only_entry() -> None:
    raw = _RawDemoClient()
    control = _Control([_decision(armed=True)])
    guarded = ControlPlaneGuardedBybitDemoClient(
        raw,
        control,  # type: ignore[arg-type]
        now_provider=lambda: _NOW,
    )

    ack = guarded.place_market_order(_order(reduce_only=False))

    assert ack.accepted is True
    assert control.read_calls == 1
    assert len(raw.orders) == 1


def test_control_guard_does_not_block_reduce_only_recovery_under_halt() -> None:
    raw = _RawDemoClient()
    control = _Control([])
    guarded = ControlPlaneGuardedBybitDemoClient(
        raw,
        control,  # type: ignore[arg-type]
        now_provider=lambda: _NOW,
    )

    ack = guarded.place_market_order(_order(reduce_only=True))

    assert ack.accepted is True
    assert control.read_calls == 0
    assert raw.orders[0].reduce_only is True
