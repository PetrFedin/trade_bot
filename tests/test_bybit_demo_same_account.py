from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_demo_same_account import (
    BybitDemoAccountIdentityInspector,
    BybitDemoSameAccountStatus,
    prove_same_bybit_demo_account,
)
from app.execution.bybit_rest_policy import BybitRestProtocolError

_READONLY_KEY = "demo-readonly-key"
_READONLY_SECRET = "demo-readonly-secret"
_TRADING_KEY = "demo-trading-key"
_TRADING_SECRET = "demo-trading-secret"


class _FakeTransport:
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
        assert query_string == ""
        assert headers["X-BAPI-API-KEY"]
        self.calls.append(path)
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": dict(self.result),
        }


def _result(
    *,
    api_key: str,
    user_id: int = 123456,
    parent_uid: str = "0",
    is_master: bool = True,
    secret: str = "",
) -> dict[str, Any]:
    return {
        "apiKey": api_key,
        "secret": secret,
        "userID": user_id,
        "parentUid": parent_uid,
        "isMaster": is_master,
    }


def _inspector(api_key: str, api_secret: str, result: Mapping[str, Any]):
    transport = _FakeTransport(result)
    inspector = BybitDemoAccountIdentityInspector(
        api_key=api_key,
        api_secret=api_secret,
        transport=transport,
        clock_ms=lambda: 1_700_000_000_000,
    )
    return inspector, transport


def _proof(
    readonly_result: Mapping[str, Any],
    trading_result: Mapping[str, Any],
):
    readonly, readonly_transport = _inspector(
        _READONLY_KEY,
        _READONLY_SECRET,
        readonly_result,
    )
    trading, trading_transport = _inspector(
        _TRADING_KEY,
        _TRADING_SECRET,
        trading_result,
    )
    proof = prove_same_bybit_demo_account(readonly, trading)
    return proof, readonly_transport, trading_transport


def test_two_distinct_keys_for_same_master_account_are_verified() -> None:
    proof, readonly_transport, trading_transport = _proof(
        _result(api_key=_READONLY_KEY),
        _result(api_key=_TRADING_KEY),
    )

    assert proof.status is BybitDemoSameAccountStatus.VERIFIED_SAME_ACCOUNT
    assert proof.passed is True
    assert proof.reasons == ()
    assert proof.same_user_id is True
    assert proof.same_parent_uid is True
    assert proof.same_master_scope is True
    assert proof.authenticated_get_only is True
    assert proof.order_write_performed is False
    assert proof.order_writes_supported is False
    assert proof.live_mainnet_order_routing_allowed is False
    assert readonly_transport.calls == ["/v5/user/query-api"]
    assert trading_transport.calls == ["/v5/user/query-api"]

    serialized = str(proof.to_payload())
    assert _READONLY_KEY not in serialized
    assert _READONLY_SECRET not in serialized
    assert _TRADING_KEY not in serialized
    assert _TRADING_SECRET not in serialized
    assert "123456" not in serialized


def test_different_user_accounts_are_blocked_without_exposing_uid() -> None:
    proof, _readonly_transport, _trading_transport = _proof(
        _result(api_key=_READONLY_KEY, user_id=123456),
        _result(api_key=_TRADING_KEY, user_id=654321),
    )

    assert proof.status is BybitDemoSameAccountStatus.BLOCKED
    assert proof.passed is False
    assert proof.reasons == ("DEMO_CREDENTIAL_USER_ID_MISMATCH",)
    serialized = str(proof.to_payload())
    assert "123456" not in serialized
    assert "654321" not in serialized


def test_different_subaccount_parent_or_scope_is_blocked() -> None:
    proof, _readonly_transport, _trading_transport = _proof(
        _result(
            api_key=_READONLY_KEY,
            user_id=222,
            parent_uid="111",
            is_master=False,
        ),
        _result(
            api_key=_TRADING_KEY,
            user_id=222,
            parent_uid="333",
            is_master=False,
        ),
    )

    assert proof.status is BybitDemoSameAccountStatus.BLOCKED
    assert "DEMO_CREDENTIAL_PARENT_UID_MISMATCH" in proof.reasons


def test_master_and_subaccount_scope_mismatch_is_blocked() -> None:
    proof, _readonly_transport, _trading_transport = _proof(
        _result(api_key=_READONLY_KEY, user_id=222, parent_uid="0", is_master=True),
        _result(api_key=_TRADING_KEY, user_id=222, parent_uid="111", is_master=False),
    )

    assert proof.status is BybitDemoSameAccountStatus.BLOCKED
    assert "DEMO_CREDENTIAL_PARENT_UID_MISMATCH" in proof.reasons
    assert "DEMO_CREDENTIAL_MASTER_SCOPE_MISMATCH" in proof.reasons


def test_configured_api_key_must_match_authenticated_response() -> None:
    readonly, _transport = _inspector(
        _READONLY_KEY,
        _READONLY_SECRET,
        _result(api_key="another-key"),
    )

    with pytest.raises(BybitRestProtocolError, match="does not match"):
        readonly.inspect()


def test_nonempty_secret_marker_is_rejected() -> None:
    readonly, _transport = _inspector(
        _READONLY_KEY,
        _READONLY_SECRET,
        _result(api_key=_READONLY_KEY, secret="unexpected-secret"),
    )

    with pytest.raises(BybitRestProtocolError, match="secret marker"):
        readonly.inspect()


@pytest.mark.parametrize(
    "override",
    [
        {"userID": 0},
        {"userID": True},
        {"parentUid": "not-an-int"},
        {"isMaster": "true"},
        {"parentUid": "99", "isMaster": True},
        {"parentUid": "0", "isMaster": False},
    ],
)
def test_invalid_account_identity_shapes_fail_closed(override: dict[str, Any]) -> None:
    result = _result(api_key=_READONLY_KEY)
    result.update(override)
    readonly, _transport = _inspector(_READONLY_KEY, _READONLY_SECRET, result)

    with pytest.raises((ValueError, BybitRestProtocolError)):
        readonly.inspect()
