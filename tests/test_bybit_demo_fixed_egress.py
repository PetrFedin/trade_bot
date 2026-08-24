from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_demo_account_reader import (
    BybitDemoAccountInfo,
    BybitDemoWalletBalance,
)
from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightStatus,
    BybitDemoOperationalDatabaseState,
    BybitDemoReadOnlyApiKeyInfo,
)
from app.execution.bybit_demo_fixed_egress import (
    BybitDemoFixedEgressPreflightAccountClient,
    FixedEgressPostgresBybitDemoControlPlane,
    require_fixed_egress_ready_for_arm,
    run_bybit_demo_fixed_egress_connected_preflight,
)

_NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


class _Transport:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        self.calls: list[str] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        del query_string, headers
        self.calls.append(path)
        return {"retCode": 0, "retMsg": "OK", "result": self.result}


class _Account(BybitDemoFixedEgressPreflightAccountClient):
    def __init__(self, *, ip_bound: bool) -> None:
        self._ip_bound = ip_bound

    def get_api_key_info(self) -> BybitDemoReadOnlyApiKeyInfo:
        return BybitDemoReadOnlyApiKeyInfo(
            read_only=True,
            ip_binding_present=self._ip_bound,
        )

    def get_wallet_balance(self) -> BybitDemoWalletBalance:
        return BybitDemoWalletBalance(
            total_equity_usd=Decimal("1000"),
            total_wallet_balance_usd=Decimal("1000"),
            total_margin_balance_usd=Decimal("1000"),
            total_available_balance_usd=Decimal("900"),
            total_perp_upl_usd=Decimal("0"),
            total_initial_margin_usd=Decimal("0"),
            total_maintenance_margin_usd=Decimal("0"),
            usdt_wallet_balance=Decimal("1000"),
        )

    def get_account_info(self) -> BybitDemoAccountInfo:
        return BybitDemoAccountInfo(
            margin_mode="REGULAR_MARGIN",
            unified_margin_status=6,
            updated_time_ms=1,
        )

    def get_open_positions(self):
        return ()

    def get_open_orders(self):
        return ()


class _Database:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    schema_mutation_supported = False

    def read_state(self) -> BybitDemoOperationalDatabaseState:
        return BybitDemoOperationalDatabaseState(
            required_relations_present=True,
            append_only_triggers_present=True,
            runtime_lease_present=False,
            active_checkpoint_order_link_id=None,
            active_checkpoint_symbol=None,
            active_checkpoint_side=None,
            active_checkpoint_entry_price=None,
            active_checkpoint_current_quantity=None,
            approval_record_count=0,
            provenance_record_count=0,
            terminal_record_count=0,
        )


def test_fixed_egress_key_info_accepts_concrete_unique_ips() -> None:
    transport = _Transport(
        {
            "readOnly": 1,
            "ips": ["203.0.113.10", "2001:db8::10"],
        }
    )
    client = BybitDemoFixedEgressPreflightAccountClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1,
    )

    info = client.get_api_key_info()

    assert info.read_only is True
    assert info.ip_binding_present is True
    assert transport.calls == ["/v5/user/query-api"]
    assert client.fixed_egress_required is True
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False


@pytest.mark.parametrize(
    "ips",
    [
        ["not-an-ip"],
        ["203.0.113.10", "203.0.113.10"],
    ],
)
def test_fixed_egress_key_info_rejects_invalid_or_duplicate_ips(ips: list[str]) -> None:
    transport = _Transport({"readOnly": 1, "ips": ips})
    client = BybitDemoFixedEgressPreflightAccountClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1,
    )

    with pytest.raises(ValueError, match="IP binding"):
        client.get_api_key_info()


def test_wildcard_or_empty_binding_is_not_fixed_egress() -> None:
    for ips in ([], ["*"], [""]):
        transport = _Transport({"readOnly": 1, "ips": ips})
        client = BybitDemoFixedEgressPreflightAccountClient(
            api_key="key",
            api_secret="secret",
            transport=transport,
            clock_ms=lambda: 1,
        )
        info = client.get_api_key_info()
        assert info.ip_binding_present is False


def test_missing_ip_binding_converts_clean_preflight_to_blocked() -> None:
    result = run_bybit_demo_fixed_egress_connected_preflight(
        _Account(ip_bound=False),
        _Database(),  # type: ignore[arg-type]
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert result.reasons == ("DEMO_READONLY_API_KEY_HAS_NO_IP_BINDING",)
    assert result.api_key_ip_binding_present is False
    assert result.trade_actionable is False
    assert result.order_writes_supported is False
    assert result.live_mainnet_order_routing_allowed is False


def test_ip_bound_clean_preflight_remains_ready_and_arm_guard_accepts() -> None:
    result = run_bybit_demo_fixed_egress_connected_preflight(
        _Account(ip_bound=True),
        _Database(),  # type: ignore[arg-type]
    )

    assert result.status is BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
    assert result.reasons == ()
    assert result.api_key_ip_binding_present is True
    require_fixed_egress_ready_for_arm(result)


def test_arm_guard_rejects_non_bound_result_even_if_status_is_forged_ready() -> None:
    blocked = run_bybit_demo_fixed_egress_connected_preflight(
        _Account(ip_bound=False),
        _Database(),  # type: ignore[arg-type]
    )
    forged = replace(
        blocked,
        status=BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL,
        reasons=(),
    )

    with pytest.raises(ValueError, match="IP-bound"):
        require_fixed_egress_ready_for_arm(forged)


def test_operational_control_plane_rejects_forged_ready_without_ip_before_database() -> None:
    blocked = run_bybit_demo_fixed_egress_connected_preflight(
        _Account(ip_bound=False),
        _Database(),  # type: ignore[arg-type]
    )
    forged = replace(
        blocked,
        status=BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL,
        reasons=(),
    )
    plane = FixedEgressPostgresBybitDemoControlPlane("postgresql://not-used")

    with pytest.raises(ValueError, match="IP-bound"):
        plane.arm_new_entries(
            forged,
            operator_id="operator",
            reason="must reject before database access",
            now=_NOW,
            preflight_observed_at=_NOW,
        )
