from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_demo_trading_credential_preflight import (
    BybitDemoTradingCredentialPreflightStatus,
    BybitDemoTradingCredentialReadOnlyInspector,
    run_bybit_demo_trading_credential_preflight,
)
from app.execution.bybit_rest_policy import BybitRestProtocolError

_TRADING_KEY = "demo-trading-key"
_TRADING_SECRET = "demo-trading-secret"
_DEMO_READONLY_KEY = "demo-readonly-key"
_MAINNET_READONLY_KEY = "mainnet-readonly-key"


class _FakeTransport:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append((path, query_string, dict(headers)))
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": dict(self.result),
        }


def _api_result(
    *,
    api_key: str = _TRADING_KEY,
    read_only: int = 0,
    ips: list[str] | None = None,
    key_type: int = 1,
    uta: int = 1,
    contract_permissions: list[str] | None = None,
    wallet_permissions: list[str] | None = None,
    spot_permissions: list[str] | None = None,
    secret: str = "",
) -> dict[str, Any]:
    return {
        "apiKey": api_key,
        "readOnly": read_only,
        "ips": ["203.0.113.10"] if ips is None else ips,
        "type": key_type,
        "uta": uta,
        "secret": secret,
        "permissions": {
            "ContractTrade": (
                ["Order", "Position"]
                if contract_permissions is None
                else contract_permissions
            ),
            "Spot": [] if spot_permissions is None else spot_permissions,
            "Wallet": [] if wallet_permissions is None else wallet_permissions,
            "Options": [],
            "Derivatives": [],
            "Exchange": [],
            "Earn": [],
        },
    }


def _client(result: Mapping[str, Any]) -> tuple[BybitDemoTradingCredentialReadOnlyInspector, _FakeTransport]:
    transport = _FakeTransport(result)
    client = BybitDemoTradingCredentialReadOnlyInspector(
        api_key=_TRADING_KEY,
        api_secret=_TRADING_SECRET,
        transport=transport,
        clock_ms=lambda: 1_700_000_000_000,
    )
    return client, transport


def _run(result: Mapping[str, Any]):
    client, transport = _client(result)
    preflight = run_bybit_demo_trading_credential_preflight(
        client,
        demo_readonly_api_key=_DEMO_READONLY_KEY,
        mainnet_readonly_api_key=_MAINNET_READONLY_KEY,
    )
    return preflight, client, transport


def test_minimal_personal_ip_bound_uta_contract_key_is_ready() -> None:
    result, client, transport = _run(_api_result())

    assert (
        result.status
        is BybitDemoTradingCredentialPreflightStatus.READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL
    )
    assert result.passed is True
    assert result.reasons == ()
    assert result.write_enabled_verified is True
    assert result.ip_binding_present is True
    assert result.personal_key_type_verified is True
    assert result.uta_enabled is True
    assert result.contract_order_permission is True
    assert result.contract_position_permission is True
    assert result.least_privilege_contract_only is True
    assert result.distinct_from_demo_readonly_key is True
    assert result.distinct_from_mainnet_readonly_key is True
    assert result.authenticated_get_only is True
    assert result.order_write_performed is False
    assert result.order_writes_supported is False
    assert result.live_mainnet_order_routing_allowed is False
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "/v5/user/query-api"
    assert transport.calls[0][1] == ""
    for mutation_name in (
        "place_market_order",
        "cancel_order",
        "amend_order",
        "set_trading_stop",
    ):
        assert not callable(getattr(client, mutation_name, None))

    payload = result.to_payload()
    serialized = str(payload)
    assert _TRADING_KEY not in serialized
    assert _TRADING_SECRET not in serialized
    assert "203.0.113.10" not in serialized
    assert "Withdraw" not in serialized


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"read_only": 1}, "DEMO_TRADING_KEY_IS_READ_ONLY"),
        ({"ips": []}, "DEMO_TRADING_KEY_HAS_NO_IP_BINDING"),
        ({"key_type": 2}, "DEMO_TRADING_KEY_IS_NOT_PERSONAL"),
        ({"uta": 0}, "DEMO_TRADING_KEY_ACCOUNT_IS_NOT_UTA"),
        (
            {"contract_permissions": ["Position"]},
            "DEMO_TRADING_KEY_MISSING_CONTRACT_ORDER_PERMISSION",
        ),
        (
            {"contract_permissions": ["Order"]},
            "DEMO_TRADING_KEY_MISSING_CONTRACT_POSITION_PERMISSION",
        ),
        (
            {"wallet_permissions": ["Withdraw"]},
            "DEMO_TRADING_KEY_IS_NOT_CONTRACT_ONLY_LEAST_PRIVILEGE",
        ),
        (
            {"spot_permissions": ["SpotTrade"]},
            "DEMO_TRADING_KEY_IS_NOT_CONTRACT_ONLY_LEAST_PRIVILEGE",
        ),
    ],
)
def test_unsafe_credential_shapes_are_blocked(
    override: dict[str, Any],
    reason: str,
) -> None:
    result, _client_value, transport = _run(_api_result(**override))

    assert result.status is BybitDemoTradingCredentialPreflightStatus.BLOCKED
    assert result.passed is False
    assert reason in result.reasons
    assert result.order_write_performed is False
    assert len(transport.calls) == 1


def test_demo_readonly_key_reuse_is_blocked() -> None:
    client, transport = _client(_api_result())
    result = run_bybit_demo_trading_credential_preflight(
        client,
        demo_readonly_api_key=_TRADING_KEY,
        mainnet_readonly_api_key=_MAINNET_READONLY_KEY,
    )

    assert result.status is BybitDemoTradingCredentialPreflightStatus.BLOCKED
    assert "DEMO_TRADING_KEY_REUSES_DEMO_READONLY_KEY" in result.reasons
    assert len(transport.calls) == 1


def test_mainnet_readonly_key_reuse_is_blocked() -> None:
    client, transport = _client(_api_result())
    result = run_bybit_demo_trading_credential_preflight(
        client,
        demo_readonly_api_key=_DEMO_READONLY_KEY,
        mainnet_readonly_api_key=_TRADING_KEY,
    )

    assert result.status is BybitDemoTradingCredentialPreflightStatus.BLOCKED
    assert "DEMO_TRADING_KEY_REUSES_MAINNET_READONLY_KEY" in result.reasons
    assert len(transport.calls) == 1


def test_api_key_identity_mismatch_is_protocol_error() -> None:
    client, _transport = _client(_api_result(api_key="another-demo-key"))

    with pytest.raises(BybitRestProtocolError, match="identity"):
        run_bybit_demo_trading_credential_preflight(
            client,
            demo_readonly_api_key=_DEMO_READONLY_KEY,
            mainnet_readonly_api_key=_MAINNET_READONLY_KEY,
        )


def test_nonempty_secret_marker_is_protocol_error() -> None:
    client, _transport = _client(_api_result(secret="unexpected"))

    with pytest.raises(BybitRestProtocolError, match="secret material"):
        run_bybit_demo_trading_credential_preflight(
            client,
            demo_readonly_api_key=_DEMO_READONLY_KEY,
            mainnet_readonly_api_key=_MAINNET_READONLY_KEY,
        )


@pytest.mark.parametrize(
    "ips",
    [["*"], ["not-an-ip"], ["203.0.113.10", "203.0.113.10"]],
)
def test_invalid_ip_bindings_are_protocol_errors(ips: list[str]) -> None:
    client, _transport = _client(_api_result(ips=ips))

    with pytest.raises(BybitRestProtocolError, match="IP binding"):
        run_bybit_demo_trading_credential_preflight(
            client,
            demo_readonly_api_key=_DEMO_READONLY_KEY,
            mainnet_readonly_api_key=_MAINNET_READONLY_KEY,
        )


def test_unknown_nonempty_permission_category_is_blocked_fail_closed() -> None:
    raw = _api_result()
    raw["permissions"]["FuturePrivilege"] = ["FutureWrite"]
    result, _client_value, _transport = _run(raw)

    assert result.status is BybitDemoTradingCredentialPreflightStatus.BLOCKED
    assert "DEMO_TRADING_KEY_IS_NOT_CONTRACT_ONLY_LEAST_PRIVILEGE" in result.reasons


def test_duplicate_contract_permissions_are_protocol_error() -> None:
    client, _transport = _client(
        _api_result(contract_permissions=["Order", "Position", "Order"])
    )

    with pytest.raises(BybitRestProtocolError, match="duplicates"):
        run_bybit_demo_trading_credential_preflight(
            client,
            demo_readonly_api_key=_DEMO_READONLY_KEY,
            mainnet_readonly_api_key=_MAINNET_READONLY_KEY,
        )
