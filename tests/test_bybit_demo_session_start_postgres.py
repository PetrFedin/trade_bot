from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.execution.bybit_demo_account_reader import (
    BybitDemoAccountInfo,
    BybitDemoWalletBalance,
)
from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightResult,
    BybitDemoConnectedPreflightStatus,
    BybitDemoReadOnlyApiKeyInfo,
    BybitDemoReadOnlyOpenOrder,
    BybitDemoReadOnlyOpenPosition,
)
from app.execution.bybit_demo_control_plane import PostgresBybitDemoControlPlane
from app.execution.bybit_demo_postgres_bootstrap import apply_bybit_demo_postgres_bootstrap
from app.execution.bybit_demo_session_start import (
    BybitDemoSessionStartStatus,
    PostgresBybitDemoSessionStartCoordinator,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_SESSION_START_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_SESSION_START_TEST_DSN is not configured",
)
_NOW = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
_GIT_SHA = "a" * 40


class _Account:
    host = "api-demo.bybit.com"
    fixed_egress_required = True
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        positions: tuple[BybitDemoReadOnlyOpenPosition, ...] = (),
        orders: tuple[BybitDemoReadOnlyOpenOrder, ...] = (),
        total_equity: Decimal = Decimal("1234.56"),
    ) -> None:
        self.positions = positions
        self.orders = orders
        self.total_equity = total_equity
        self.calls: list[str] = []

    def get_api_key_info(self) -> BybitDemoReadOnlyApiKeyInfo:
        self.calls.append("api-key")
        return BybitDemoReadOnlyApiKeyInfo(
            read_only=True,
            ip_binding_present=True,
        )

    def get_wallet_balance(self) -> BybitDemoWalletBalance:
        self.calls.append("wallet")
        return BybitDemoWalletBalance(
            total_equity_usd=self.total_equity,
            total_wallet_balance_usd=self.total_equity,
            total_margin_balance_usd=self.total_equity,
            total_available_balance_usd=self.total_equity,
            total_perp_upl_usd=Decimal("0"),
            total_initial_margin_usd=Decimal("0"),
            total_maintenance_margin_usd=Decimal("0"),
            usdt_wallet_balance=self.total_equity,
        )

    def get_account_info(self) -> BybitDemoAccountInfo:
        self.calls.append("account")
        return BybitDemoAccountInfo(
            margin_mode="REGULAR_MARGIN",
            unified_margin_status=6,
            updated_time_ms=1,
        )

    def get_open_positions(self) -> tuple[BybitDemoReadOnlyOpenPosition, ...]:
        self.calls.append("positions")
        return self.positions

    def get_open_orders(self) -> tuple[BybitDemoReadOnlyOpenOrder, ...]:
        self.calls.append("orders")
        return self.orders

    def has_entry_execution(
        self,
        *,
        symbol: str,
        side: str,
        entry_order_link_id: str,
    ) -> bool:
        self.calls.append(f"execution:{symbol}:{side}:{entry_order_link_id}")
        return False


def _clean_preflight() -> BybitDemoConnectedPreflightResult:
    return BybitDemoConnectedPreflightResult(
        status=BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL,
        reasons=(),
        margin_mode="REGULAR_MARGIN",
        unified_margin_status=6,
        positive_equity=True,
        positive_available_balance=True,
        usdt_wallet_visible=True,
        open_position_count=0,
        open_position_symbols=(),
        open_order_count=0,
        open_order_symbols=(),
        active_checkpoint_present=False,
        active_checkpoint_symbol=None,
        runtime_lease_present=False,
        required_relations_present=True,
        append_only_triggers_present=True,
        approval_record_count=0,
        provenance_record_count=0,
        terminal_record_count=0,
        read_only_api_key_verified=True,
        api_key_ip_binding_present=True,
    )


def _position() -> BybitDemoReadOnlyOpenPosition:
    return BybitDemoReadOnlyOpenPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("0.01"),
        average_price=Decimal("60000"),
    )


def test_session_start_is_one_time_flat_halted_and_restart_safe() -> None:
    applied = apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V122",
    )
    assert applied.passed is True

    coordinator = PostgresBybitDemoSessionStartCoordinator(_DSN)
    before = coordinator.read_status()
    assert before.status is BybitDemoSessionStartStatus.NOT_INITIALIZED
    assert before.session_initialized is False
    assert before.worker_session_ready is False
    assert before.automatic_reset_allowed is False
    assert before.order_writes_supported is False
    assert before.live_mainnet_order_routing_allowed is False

    wrong = _Account()
    with pytest.raises(ValueError, match="confirmation phrase"):
        coordinator.initialize(
            wrong,
            confirmation_phrase="WRONG",
            operator_id="operator-a",
            reason="initial Demo risk session",
            git_sha=_GIT_SHA,
            now=_NOW,
        )
    assert wrong.calls == []

    plane = PostgresBybitDemoControlPlane(_DSN)
    plane.arm_new_entries(
        _clean_preflight(),
        operator_id="operator-a",
        reason="prove initialization rejects ARMED control",
        now=_NOW,
        preflight_observed_at=_NOW,
        ttl_seconds=120,
    )
    armed_attempt = coordinator.initialize(
        _Account(),
        confirmation_phrase="INITIALIZE_BYBIT_DEMO_SESSION_RISK",
        operator_id="operator-a",
        reason="must be halted",
        git_sha=_GIT_SHA,
        now=_NOW,
    )
    assert armed_attempt.status is BybitDemoSessionStartStatus.BLOCKED
    assert armed_attempt.reasons == ("DEMO_SESSION_CONTROL_NOT_HALTED",)
    plane.halt_new_entries(
        operator_id="operator-a",
        reason="restore HALT before session initialization",
        now=_NOW,
    )

    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_runtime_lease_v119(
                   lease_name, owner_token, created_time_ms, process_id, created_at
               ) VALUES ('CANONICAL_DEMO_TRADING_RUNTIME', %s, 1, 1, %s)""",
            ("b" * 64, _NOW),
        )
    lease_attempt = coordinator.initialize(
        _Account(),
        confirmation_phrase="INITIALIZE_BYBIT_DEMO_SESSION_RISK",
        operator_id="operator-a",
        reason="must reject active runtime",
        git_sha=_GIT_SHA,
        now=_NOW,
    )
    assert lease_attempt.status is BybitDemoSessionStartStatus.BLOCKED
    assert lease_attempt.reasons == ("DEMO_SESSION_RUNTIME_LEASE_PRESENT",)
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM astra_bybit_demo_runtime_lease_v119 WHERE owner_token=%s",
            ("b" * 64,),
        )

    positioned = coordinator.initialize(
        _Account(positions=(_position(),)),
        confirmation_phrase="INITIALIZE_BYBIT_DEMO_SESSION_RISK",
        operator_id="operator-a",
        reason="must reject non-flat account",
        git_sha=_GIT_SHA,
        now=_NOW,
    )
    assert positioned.status is BybitDemoSessionStartStatus.BLOCKED
    assert positioned.reasons == ("DEMO_SESSION_CONNECTED_PREFLIGHT_NOT_READY",)

    clean = _Account()
    initialized = coordinator.initialize(
        clean,
        confirmation_phrase="INITIALIZE_BYBIT_DEMO_SESSION_RISK",
        operator_id="operator-a",
        reason="start durable Demo risk session",
        git_sha=_GIT_SHA,
        now=_NOW,
    )
    assert initialized.status is BybitDemoSessionStartStatus.INITIALIZED_NOW
    assert initialized.session_initialized is True
    assert initialized.worker_session_ready is True
    assert initialized.opening_equity_positive is True
    assert initialized.outcome_count == 0
    assert initialized.ledger_revision_sha256 is not None
    assert len(initialized.ledger_revision_sha256) == 64
    assert initialized.preflight_record_sha256 is not None
    assert len(initialized.preflight_record_sha256) == 64
    assert initialized.session_start_id is not None
    assert len(initialized.session_start_id) == 64
    assert initialized.git_sha == _GIT_SHA
    assert initialized.trading_credential_required is False
    assert initialized.order_write_performed is False
    assert clean.calls.count("wallet") >= 2
    assert clean.calls[-2:] == ["positions", "orders"]
    serialized = json.dumps(initialized.to_payload(), sort_keys=True)
    assert "1234.56" not in serialized

    restarted = PostgresBybitDemoSessionStartCoordinator(_DSN)
    resumed = restarted.read_status()
    assert resumed.status is BybitDemoSessionStartStatus.INITIALIZED
    assert resumed.worker_session_ready is True
    assert resumed.ledger_revision_sha256 == initialized.ledger_revision_sha256
    assert resumed.outcome_count == 0

    second = restarted.initialize(
        _Account(total_equity=Decimal("9999")),
        confirmation_phrase="INITIALIZE_BYBIT_DEMO_SESSION_RISK",
        operator_id="operator-b",
        reason="must not replace existing session",
        git_sha="c" * 40,
        now=_NOW,
    )
    assert second.status is BybitDemoSessionStartStatus.BLOCKED
    assert second.reasons == ("DEMO_SESSION_ALREADY_INITIALIZED",)
    assert second.session_initialized is True
    assert second.worker_session_ready is True
    assert second.ledger_revision_sha256 == initialized.ledger_revision_sha256

    with psycopg.connect(_DSN) as connection:
        row = connection.execute(
            """SELECT opening_equity_usdt, outcome_count
               FROM astra_bybit_demo_session_risk_v122
               WHERE session_name='ACTIVE'"""
        ).fetchone()
    assert row is not None
    assert row[0] == Decimal("1234.56")
    assert int(row[1]) == 0
