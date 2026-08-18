import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient


class _FakeTransport:
    def __init__(self, pages: list[Mapping[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append((path, query_string, dict(headers)))
        return self.pages.pop(0)


def _account_info(
    *,
    margin_mode: str = "REGULAR_MARGIN",
    unified_margin_status: object = 5,
    updated_time: object = "1787076000000",
) -> dict[str, object]:
    return {
        "retCode": 0,
        "result": {
            "marginMode": margin_mode,
            "unifiedMarginStatus": unified_margin_status,
            "updatedTime": updated_time,
        },
    }


def test_account_info_reader_signs_empty_query_and_parses_margin_mode() -> None:
    transport = _FakeTransport([_account_info()])
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1234567890,
        recv_window_ms=5000,
    )

    info = client.get_account_info()

    assert info.margin_mode == "REGULAR_MARGIN"
    assert info.unified_margin_status == 5
    assert info.updated_time_ms == 1787076000000
    path, query, headers = transport.calls[0]
    assert path == "/v5/account/info"
    assert query == ""
    payload = "1234567890" + "key" + "5000"
    expected = hmac.new(
        b"secret",
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False


@pytest.mark.parametrize(
    "margin_mode",
    ["ISOLATED_MARGIN", "REGULAR_MARGIN", "PORTFOLIO_MARGIN"],
)
def test_account_info_reader_accepts_documented_margin_modes(margin_mode: str) -> None:
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=_FakeTransport([_account_info(margin_mode=margin_mode)]),
    )
    assert client.get_account_info().margin_mode == margin_mode


def test_account_info_reader_rejects_unknown_margin_mode() -> None:
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=_FakeTransport([_account_info(margin_mode="UNKNOWN")]),
    )
    with pytest.raises(ValueError, match="unsupported margin mode"):
        client.get_account_info()


@pytest.mark.parametrize(
    ("status", "updated_time"),
    [(0, "1"), (-1, "1"), (True, "1"), (5, -1), (5, "not-a-time")],
)
def test_account_info_reader_rejects_invalid_status_or_time(
    status: object,
    updated_time: object,
) -> None:
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=_FakeTransport(
            [_account_info(unified_margin_status=status, updated_time=updated_time)]
        ),
    )
    with pytest.raises(ValueError):
        client.get_account_info()
