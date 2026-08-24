from __future__ import annotations

import ipaddress
from dataclasses import replace
from datetime import datetime
from typing import Any

from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightResult,
    BybitDemoConnectedPreflightStatus,
    BybitDemoPreflightAccountClient,
    BybitDemoReadOnlyApiKeyInfo,
    PostgresBybitDemoOperationalStateReader,
    run_bybit_demo_connected_preflight,
)
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlEventReceipt,
    PostgresBybitDemoControlPlane,
)


class BybitDemoFixedEgressPreflightAccountClient(BybitDemoPreflightAccountClient):
    """Read-only Demo preflight client with strict fixed-egress key binding validation."""

    fixed_egress_required = True
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def get_api_key_info(self) -> BybitDemoReadOnlyApiKeyInfo:
        result = self._private_get_result(  # noqa: SLF001 - bounded GET-only subclass.
            path="/v5/user/query-api",
            query={},
        )
        raw_read_only = result.get("readOnly")
        if isinstance(raw_read_only, bool) or raw_read_only not in {0, 1}:
            raise ValueError("Bybit Demo fixed-egress API key readOnly flag is invalid")
        raw_ips = result.get("ips")
        if not isinstance(raw_ips, list) or any(not isinstance(ip, str) for ip in raw_ips):
            raise ValueError("Bybit Demo fixed-egress API key IP binding list is invalid")

        normalized: list[str] = []
        for raw_ip in raw_ips:
            if not raw_ip or raw_ip == "*":
                continue
            try:
                normalized_ip = str(ipaddress.ip_address(raw_ip))
            except ValueError as exc:
                raise ValueError("Bybit Demo fixed-egress API key IP binding is invalid") from exc
            normalized.append(normalized_ip)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Bybit Demo fixed-egress API key IP bindings contain duplicates")

        return BybitDemoReadOnlyApiKeyInfo(
            read_only=raw_read_only == 1,
            ip_binding_present=bool(normalized),
        )


def run_bybit_demo_fixed_egress_connected_preflight(
    account_client: BybitDemoFixedEgressPreflightAccountClient,
    database_reader: PostgresBybitDemoOperationalStateReader,
) -> BybitDemoConnectedPreflightResult:
    """Run canonical connected preflight and require a concrete IP-bound read-only key."""

    _validate_fixed_egress_client(account_client)
    result = run_bybit_demo_connected_preflight(account_client, database_reader)
    if result.api_key_ip_binding_present:
        return result
    reason = "DEMO_READONLY_API_KEY_HAS_NO_IP_BINDING"
    reasons = result.reasons if reason in result.reasons else (*result.reasons, reason)
    return replace(
        result,
        status=BybitDemoConnectedPreflightStatus.BLOCKED,
        reasons=reasons,
    )


def require_fixed_egress_ready_for_arm(result: BybitDemoConnectedPreflightResult) -> None:
    """Defense-in-depth guard used by operator ARM paths before durable control mutation."""

    if (
        result.status
        is not BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
    ):
        raise ValueError("Bybit Demo ARM requires fixed-egress connected preflight readiness")
    if not result.read_only_api_key_verified:
        raise ValueError("Bybit Demo ARM requires verified read-only API key")
    if not result.api_key_ip_binding_present:
        raise ValueError("Bybit Demo ARM requires IP-bound read-only API key")
    if result.reasons:
        raise ValueError("Bybit Demo ARM rejected connected preflight reasons")
    if result.order_writes_supported or result.live_mainnet_order_routing_allowed:
        raise ValueError("Bybit Demo ARM rejected unsafe connected preflight capability")


class FixedEgressPostgresBybitDemoControlPlane(PostgresBybitDemoControlPlane):
    """Operational v121 plane that cannot persist ARM from a non-zone-bound preflight."""

    fixed_egress_required = True

    def arm_new_entries(
        self,
        preflight: BybitDemoConnectedPreflightResult,
        *,
        operator_id: str,
        reason: str,
        now: datetime,
        preflight_observed_at: datetime,
        ttl_seconds: int = 120,
    ) -> BybitDemoControlEventReceipt:
        require_fixed_egress_ready_for_arm(preflight)
        return super().arm_new_entries(
            preflight,
            operator_id=operator_id,
            reason=reason,
            now=now,
            preflight_observed_at=preflight_observed_at,
            ttl_seconds=ttl_seconds,
        )


def _validate_fixed_egress_client(client: Any) -> None:
    if getattr(client, "host", None) != "api-demo.bybit.com":
        raise ValueError("Bybit Demo fixed-egress preflight rejected non-demo host")
    if getattr(client, "fixed_egress_required", False) is not True:
        raise ValueError("Bybit Demo fixed-egress preflight client is not zone-bound")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("Bybit Demo fixed-egress preflight rejected mainnet capability")
    if getattr(client, "order_writes_supported", True) is not False:
        raise ValueError("Bybit Demo fixed-egress preflight must remain order-read-only")
    for name in ("place_order", "cancel_order", "place_market_order", "amend_order"):
        if callable(getattr(client, name, None)):
            raise ValueError("Bybit Demo fixed-egress preflight exposes mutation method")


__all__ = [
    "BybitDemoFixedEgressPreflightAccountClient",
    "FixedEgressPostgresBybitDemoControlPlane",
    "require_fixed_egress_ready_for_arm",
    "run_bybit_demo_fixed_egress_connected_preflight",
]
