import hashlib
import hmac
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetReadOnlyClient,
    BybitMainnetReadOnlyError,
)
from app.runtime.bybit_mainnet_readonly_probe import (
    BybitMainnetReadOnlyConfigError,
    BybitMainnetReadOnlyCredentials,
    probe_bybit_mainnet_readonly_connection,
)


class _FakeTransport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append((path, query_string, dict(headers)))
        return self.responses.pop(0)


def _ok_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": dict(result)}


def _api_key_result(
    *,
    read_only: int = 1,
    ips: list[str] | None = None,
    api_key: str = "key",
) -> dict[str, Any]:
    return _ok_result(
        {
            "id": "123",
            "note": "astra-readonly",
            "apiKey": api_key,
            "readOnly": read_only,
            "secret": "",
            "permissions": {
                "ContractTrade": ["Order", "Position"],
                "Wallet": [],
            },
            "ips": ["203.0.113.10"] if ips is None else ips,
            "type": 1,
        }
    )


def _account_result() -> dict[str, Any]:
    return _ok_result(
        {
            "marginMode": "REGULAR_MARGIN",
            "unifiedMarginStatus": 6,
            "updatedTime": "1787414400000",
        }
    )


def _wallet_result() -> dict[str, Any]:
    return _ok_result(
        {
            "list": [
                {
                    "accountType": "UNIFIED",
                    "totalEquity": "2500.25",
                    "totalWalletBalance": "2475.00",
                    "totalMarginBalance": "2500.25",
                    "totalAvailableBalance": "2200.00",
                    "totalPerpUPL": "25.25",
                    "totalInitialMargin": "250.00",
                    "totalMaintenanceMargin": "25.00",
                    "coin": [
                        {"coin": "USDT", "walletBalance": "2475.00"},
                        {"coin": "BTC", "walletBalance": "0.001"},
                    ],
                }
            ]
        }
    )


def _positions_result() -> dict[str, Any]:
    return _ok_result(
        {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "0.01",
                    "positionIdx": 0,
                    "avgPrice": "100000",
                    "markPrice": "101000",
                    "positionValue": "1010",
                    "unrealisedPnl": "10",
                    "liqPrice": "50000",
                    "leverage": "2",
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "",
                    "size": "0",
                    "positionIdx": 0,
                    "avgPrice": "",
                    "markPrice": "4000",
                    "positionValue": "0",
                    "unrealisedPnl": "0",
                    "liqPrice": "",
                    "leverage": "",
                },
            ],
            "nextPageCursor": "",
        }
    )


def test_readonly_key_verification_signs_exact_mainnet_get_and_returns_safe_metadata() -> None:
    transport = _FakeTransport([_api_key_result()])
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1234567890,
        recv_window_ms=5000,
    )

    info = client.verify_read_only_api_key()

    assert client.host == "api.bybit.com"
    assert client.environment == "BYBIT_MAINNET_READONLY"
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert info.read_only is True
    assert info.ip_bindings == ("203.0.113.10",)
    assert info.permissions == ("ContractTrade:Order", "ContractTrade:Position")
    assert info.key_fingerprint_sha256 == hashlib.sha256(b"key").hexdigest()
    path, query, headers = transport.calls[0]
    assert path == "/v5/user/query-api"
    assert query == ""
    payload = "1234567890" + "key" + "5000"
    expected = hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected
    assert headers["X-BAPI-API-KEY"] == "key"
    assert not hasattr(client, "place_order")
    assert not hasattr(client, "place_market_order")
    assert not hasattr(client, "cancel_order")
    assert not hasattr(client, "set_full_position_protection")


def test_readonly_boundary_rejects_read_write_key_before_account_reads() -> None:
    transport = _FakeTransport([_api_key_result(read_only=0)])
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
    )

    with pytest.raises(BybitMainnetReadOnlyError, match="read-only"):
        client.verify_read_only_api_key()

    assert len(transport.calls) == 1


def test_readonly_boundary_requires_server_ip_binding_for_production_probe() -> None:
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=_FakeTransport([_api_key_result(ips=[])]),
    )

    with pytest.raises(BybitMainnetReadOnlyError, match="server IP"):
        client.verify_read_only_api_key(require_ip_binding=True)


def test_readonly_boundary_blocks_mutation_path_even_through_internal_get() -> None:
    transport = _FakeTransport([])
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
    )

    with pytest.raises(BybitMainnetReadOnlyError, match="allowlist"):
        client._private_get_result(path="/v5/order/create", query={})

    assert transport.calls == []


def test_wallet_and_position_reads_parse_mainnet_account_without_write_surface() -> None:
    transport = _FakeTransport([_wallet_result(), _positions_result()])
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1234567890,
    )

    wallet = client.get_wallet_balance()
    positions = client.get_positions()

    assert wallet.total_equity_usd == Decimal("2500.25")
    assert wallet.usdt_wallet_balance == Decimal("2475.00")
    assert len(positions) == 1
    position = positions[0]
    assert position.symbol == "BTCUSDT"
    assert position.side == "Buy"
    assert position.size == Decimal("0.01")
    assert position.average_price == Decimal("100000")
    assert position.unrealised_pnl == Decimal("10")
    assert transport.calls[0][0] == "/v5/account/wallet-balance"
    assert transport.calls[0][1] == "accountType=UNIFIED"
    assert transport.calls[1][0] == "/v5/position/list"
    assert transport.calls[1][1] == "category=linear&limit=200&settleCoin=USDT"


def test_full_probe_verifies_key_before_reading_account_wallet_and_positions() -> None:
    transport = _FakeTransport(
        [_api_key_result(), _account_result(), _wallet_result(), _positions_result()]
    )
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1234567890,
    )

    snapshot = probe_bybit_mainnet_readonly_connection(client)
    safe = snapshot.to_safe_dict()

    assert [call[0] for call in transport.calls] == [
        "/v5/user/query-api",
        "/v5/account/info",
        "/v5/account/wallet-balance",
        "/v5/position/list",
    ]
    assert safe["environment"] == "BYBIT_MAINNET_READONLY"
    assert safe["live_mainnet_order_routing_allowed"] is False
    assert safe["order_writes_supported"] is False
    assert safe["credential_safety"]["read_only_verified"] is True
    assert safe["credential_safety"]["ip_binding_verified"] is True
    assert safe["wallet"]["total_equity_usd"] == "2500.25"
    assert safe["positions"][0]["symbol"] == "BTCUSDT"
    serialized = str(safe)
    assert "secret" not in serialized
    assert "'key'" not in serialized


def test_mainnet_credentials_use_separate_env_names_and_hide_secrets_from_repr() -> None:
    credentials = BybitMainnetReadOnlyCredentials.from_env(
        {
            "BYBIT_MAINNET_READONLY_API_KEY": "key",
            "BYBIT_MAINNET_READONLY_API_SECRET": "secret",
            "BYBIT_API_KEY": "demo-key-must-not-be-used",
            "BYBIT_API_SECRET": "demo-secret-must-not-be-used",
        }
    )

    assert credentials.api_key == "key"
    assert credentials.api_secret == "secret"
    assert "key" not in repr(credentials)
    assert "secret" not in repr(credentials)

    with pytest.raises(BybitMainnetReadOnlyConfigError):
        BybitMainnetReadOnlyCredentials.from_env(
            {"BYBIT_MAINNET_READONLY_API_KEY": "key"}
        )


@pytest.mark.parametrize(
    "api_key,api_secret",
    [
        (" key", "secret"),
        ("key", "secret\n"),
        ("placeholder", "secret"),
        ("key", "your_api_secret"),
    ],
)
def test_mainnet_credentials_fail_closed_on_unsafe_secret_values(
    api_key: str,
    api_secret: str,
) -> None:
    with pytest.raises(BybitMainnetReadOnlyConfigError):
        BybitMainnetReadOnlyCredentials.from_env(
            {
                "BYBIT_MAINNET_READONLY_API_KEY": api_key,
                "BYBIT_MAINNET_READONLY_API_SECRET": api_secret,
            }
        )
