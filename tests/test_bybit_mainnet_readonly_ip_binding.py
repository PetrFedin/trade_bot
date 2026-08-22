from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetReadOnlyClient,
    BybitMainnetReadOnlyError,
)
from app.execution.bybit_rest_policy import BybitRestProtocolError


class _Transport:
    def __init__(self, ips: list[str]) -> None:
        self.ips = ips

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        assert path == "/v5/user/query-api"
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "apiKey": "key",
                "readOnly": 1,
                "secret": "",
                "ips": self.ips,
                "type": 1,
                "permissions": {},
            },
        }


def _client(ips: list[str]) -> BybitMainnetReadOnlyClient:
    return BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=_Transport(ips),
        clock_ms=lambda: 1234567890,
    )


def test_wildcard_ip_binding_is_rejected_as_unbound() -> None:
    with pytest.raises(BybitMainnetReadOnlyError, match="wildcard"):
        _client(["*"]).verify_read_only_api_key()


def test_invalid_ip_binding_is_rejected() -> None:
    with pytest.raises(BybitRestProtocolError, match="invalid IP"):
        _client(["not-an-ip"]).verify_read_only_api_key()


def test_duplicate_ip_binding_is_rejected() -> None:
    with pytest.raises(BybitRestProtocolError, match="duplicate IP"):
        _client(["203.0.113.10", "203.0.113.10"]).verify_read_only_api_key()


def test_multiple_concrete_ipv4_and_ipv6_bindings_are_accepted() -> None:
    info = _client(["203.0.113.10", "2001:db8::10"]).verify_read_only_api_key()

    assert info.ip_bindings == ("203.0.113.10", "2001:db8::10")
