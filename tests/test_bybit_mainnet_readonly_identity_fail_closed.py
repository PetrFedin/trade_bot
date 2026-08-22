from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetReadOnlyClient,
    BybitMainnetReadOnlyError,
)
from app.execution.bybit_rest_policy import BybitRestProtocolError


class _Transport:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        self.calls = 0

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls += 1
        assert path == "/v5/user/query-api"
        assert query_string == ""
        assert headers["X-BAPI-API-KEY"] == "key"
        return {"retCode": 0, "retMsg": "OK", "result": self.result}


def _base_result() -> dict[str, Any]:
    return {
        "apiKey": "key",
        "readOnly": 1,
        "secret": "",
        "ips": ["203.0.113.10"],
        "type": 1,
        "permissions": {},
    }


def _verify(result: Mapping[str, Any]) -> None:
    client = BybitMainnetReadOnlyClient(
        api_key="key",
        api_secret="secret",
        transport=_Transport(result),
        clock_ms=lambda: 1234567890,
    )
    client.verify_read_only_api_key()


def test_api_key_identity_field_is_required() -> None:
    result = _base_result()
    result.pop("apiKey")

    with pytest.raises(BybitRestProtocolError, match="missing apiKey identity"):
        _verify(result)


def test_returned_api_key_must_match_configured_credential() -> None:
    result = _base_result()
    result["apiKey"] = "different-key"

    with pytest.raises(BybitMainnetReadOnlyError, match="identity does not match"):
        _verify(result)


def test_secret_marker_field_is_required_and_must_be_empty() -> None:
    missing = _base_result()
    missing.pop("secret")
    with pytest.raises(BybitRestProtocolError, match="secret material"):
        _verify(missing)

    nonempty = _base_result()
    nonempty["secret"] = "unexpected"
    with pytest.raises(BybitRestProtocolError, match="secret material"):
        _verify(nonempty)
