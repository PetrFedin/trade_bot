from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import pytest

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflight,
    BybitMainnetClockPreflightError,
)
from app.execution.bybit_mainnet_readonly_activity import (
    BybitMainnetActivityError,
    BybitMainnetActivitySnapshot,
    BybitMainnetActivityWindow,
    BybitMainnetReadOnlyActivityClient,
    read_bybit_mainnet_activity,
)
from app.runtime.bybit_mainnet_readonly_activity_probe import (
    probe_bybit_mainnet_readonly_activity,
)
from app.runtime.bybit_mainnet_readonly_probe import BybitMainnetReadOnlyCredentials

_START = 1_787_000_000_000
_END = _START + 60_000


class _Transport:
    def __init__(
        self,
        *,
        executions: list[Mapping[str, Any]] | None = None,
        closed_pnl: list[Mapping[str, Any]] | None = None,
        transactions: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.executions = list(executions or [])
        self.closed_pnl = list(closed_pnl or [])
        self.transactions = list(transactions or [])
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        assert headers["X-BAPI-API-KEY"] == "key"
        self.calls.append((path, parse_qs(query_string, keep_blank_values=True)))
        if path == "/v5/user/query-api":
            return _ok(
                {
                    "apiKey": "key",
                    "readOnly": 1,
                    "secret": "",
                    "ips": ["203.0.113.10"],
                    "type": 1,
                    "permissions": {},
                }
            )
        if path == "/v5/execution/list":
            return _ok({"list": self.executions, "nextPageCursor": ""})
        if path == "/v5/position/closed-pnl":
            return _ok({"list": self.closed_pnl, "nextPageCursor": ""})
        if path == "/v5/account/transaction-log":
            return _ok({"list": self.transactions, "nextPageCursor": ""})
        raise AssertionError(f"unexpected path {path}")


def _ok(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": dict(result)}


def _execution(*, exec_id: str = "exec-1", exec_fee: str = "0.55") -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "orderId": "order-1",
        "orderLinkId": "ASTRA-OBS-1",
        "side": "Buy",
        "orderType": "Market",
        "leavesQty": "0",
        "execFee": exec_fee,
        "execId": exec_id,
        "execPrice": "100000",
        "execQty": "0.01",
        "execType": "Trade",
        "execValue": "1000",
        "execTime": str(_START + 10_000),
        "feeCurrency": "",
        "isMaker": False,
        "feeRate": "0.00055",
        "closedSize": "0",
        "seq": "101",
    }


def _closed_pnl(*, symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "orderId": "close-1",
        "side": "Sell",
        "orderType": "Market",
        "execType": "Trade",
        "qty": "0.01",
        "closedSize": "0.01",
        "cumEntryValue": "1000",
        "avgEntryPrice": "100000",
        "cumExitValue": "990",
        "avgExitPrice": "99000",
        "closedPnl": "-11.0945",
        "fillCount": "1",
        "leverage": "2",
        "openFee": "0.55",
        "closeFee": "0.5445",
        "createdTime": str(_START + 20_000),
        "updatedTime": str(_START + 20_100),
    }


def _transaction(
    *,
    transaction_id: str = "tx-1",
    transaction_time_ms: int = _START + 30_000,
    trade_id: str = "trade-1",
    order_id: str = "order-1",
    cash_flow: str = "5",
    funding: str = "-1",
    fee: str = "0.5",
    change: str = "3.5",
) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "symbol": "BTCUSDT",
        "category": "linear",
        "side": "Buy",
        "transactionTime": str(transaction_time_ms),
        "type": "TRADE",
        "transSubType": "",
        "qty": "0.01",
        "size": "0.01",
        "currency": "USDT",
        "tradePrice": "100000",
        "funding": funding,
        "fee": fee,
        "cashFlow": cash_flow,
        "change": change,
        "cashBalance": "1003.5",
        "feeRate": "0.0005",
        "tradeId": trade_id,
        "orderId": order_id,
        "orderLinkId": "ASTRA-OBS-1",
    }


def _client(transport: _Transport) -> BybitMainnetReadOnlyActivityClient:
    return BybitMainnetReadOnlyActivityClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: _END,
    )


def _window() -> BybitMainnetActivityWindow:
    return BybitMainnetActivityWindow(start_time_ms=_START, end_time_ms=_END)


def _clock(*, offset_ms: int = 0) -> BybitMainnetClockPreflight:
    send = _END - 100
    receive = _END + 100
    midpoint = _END
    preflight = BybitMainnetClockPreflight(
        api_host="api.bybit.com",
        local_send_time_ms=send,
        local_receive_time_ms=receive,
        server_time_ms=midpoint + offset_ms,
        round_trip_time_ms=200,
        estimated_clock_offset_ms=offset_ms,
        uncertainty_ms=100,
        worst_case_abs_clock_skew_ms=abs(offset_ms) + 100,
    )
    preflight.validate()
    return preflight


def test_reader_uses_structurally_bounded_linear_usdt_queries() -> None:
    transport = _Transport(
        executions=[_execution()],
        closed_pnl=[_closed_pnl(), {"symbol": "BTCPERP"}],
        transactions=[_transaction()],
    )
    snapshot = read_bybit_mainnet_activity(_client(transport), window=_window())

    assert len(snapshot.executions) == 1
    assert len(snapshot.closed_pnl) == 1
    assert snapshot.excluded_non_usdt_closed_pnl_count == 1
    assert len(snapshot.transactions) == 1
    assert snapshot.transaction_cash_flow_usdt == Decimal("5")
    assert snapshot.transaction_funding_usdt == Decimal("-1")
    assert snapshot.transaction_fee_usdt == Decimal("0.5")
    assert snapshot.transaction_change_usdt == Decimal("3.5")
    assert snapshot.live_mainnet_order_routing_allowed is False
    assert snapshot.order_writes_supported is False

    assert [path for path, _ in transport.calls] == [
        "/v5/user/query-api",
        "/v5/execution/list",
        "/v5/position/closed-pnl",
        "/v5/account/transaction-log",
    ]
    execution_query = transport.calls[1][1]
    assert execution_query == {
        "category": ["linear"],
        "settleCoin": ["USDT"],
        "startTime": [str(_START)],
        "endTime": [str(_END)],
        "limit": ["100"],
    }
    closed_query = transport.calls[2][1]
    assert closed_query == {
        "category": ["linear"],
        "startTime": [str(_START)],
        "endTime": [str(_END)],
        "limit": ["100"],
    }
    transaction_query = transport.calls[3][1]
    assert transaction_query == {
        "accountType": ["UNIFIED"],
        "category": ["linear"],
        "currency": ["USDT"],
        "startTime": [str(_START)],
        "endTime": [str(_END)],
        "limit": ["50"],
    }


def test_execution_identity_is_idempotent_and_conflicts_fail_closed() -> None:
    record = _execution(exec_id="same")
    exact = read_bybit_mainnet_activity(
        _client(_Transport(executions=[record, dict(record)])),
        window=_window(),
    )
    assert len(exact.executions) == 1

    conflicting = dict(record)
    conflicting["execFee"] = "0.75"
    with pytest.raises(BybitMainnetActivityError, match="conflicting broker records"):
        read_bybit_mainnet_activity(
            _client(_Transport(executions=[record, conflicting])),
            window=_window(),
        )


def test_blank_execution_fee_currency_stays_unknown() -> None:
    snapshot = read_bybit_mainnet_activity(
        _client(_Transport(executions=[_execution()])),
        window=_window(),
    )
    assert snapshot.executions[0].fee_currency is None
    safe = snapshot.to_safe_dict()
    assert safe["executions"][0]["fee_currency"] is None
    assert "execution_fee_usdt" not in safe["summary"]


def test_transaction_accounting_identity_fails_closed_on_inconsistent_row() -> None:
    bad = _transaction(change="99")
    with pytest.raises(BybitMainnetActivityError, match="change = cashFlow"):
        read_bybit_mainnet_activity(
            _client(_Transport(transactions=[bad])),
            window=_window(),
        )


def test_activity_probe_anchors_window_to_bybit_server_time() -> None:
    events: list[str] = []
    credentials = BybitMainnetReadOnlyCredentials(
        api_key="key",
        api_secret="secret",
        site="global",
    )
    preflight = _clock()
    expected_window = BybitMainnetActivityWindow.last_24_hours_ending_at(
        preflight.server_time_ms
    )

    def clock_probe(*, host: str) -> BybitMainnetClockPreflight:
        events.append(f"clock:{host}")
        return preflight

    def activity_reader(
        client: BybitMainnetReadOnlyActivityClient,
        *,
        window: BybitMainnetActivityWindow,
    ) -> BybitMainnetActivitySnapshot:
        events.append("activity")
        assert client.host == "api.bybit.com"
        assert window == expected_window
        return BybitMainnetActivitySnapshot(
            window=window,
            api_host=client.host,
            api_key_fingerprint_sha256="a" * 64,
            executions=(),
            closed_pnl=(),
            transactions=(),
            excluded_non_usdt_closed_pnl_count=0,
            transaction_cash_flow_usdt=Decimal("0"),
            transaction_funding_usdt=Decimal("0"),
            transaction_fee_usdt=Decimal("0"),
            transaction_change_usdt=Decimal("0"),
        )

    snapshot = probe_bybit_mainnet_readonly_activity(
        credentials=credentials,
        clock_probe=clock_probe,
        activity_reader=activity_reader,
    )
    assert snapshot.window == expected_window
    assert events == ["clock:api.bybit.com", "activity"]


def test_activity_probe_blocks_private_reader_on_unsafe_clock() -> None:
    events: list[str] = []
    credentials = BybitMainnetReadOnlyCredentials(
        api_key="key",
        api_secret="secret",
        site="global",
    )

    def clock_probe(*, host: str) -> BybitMainnetClockPreflight:
        events.append(f"clock:{host}")
        return _clock(offset_ms=600)

    def activity_reader(
        client: BybitMainnetReadOnlyActivityClient,
        *,
        window: BybitMainnetActivityWindow,
    ) -> BybitMainnetActivitySnapshot:
        raise AssertionError("authenticated activity must not run")

    with pytest.raises(BybitMainnetClockPreflightError, match="CLOCK_SKEW_UNSAFE"):
        probe_bybit_mainnet_readonly_activity(
            credentials=credentials,
            clock_probe=clock_probe,
            activity_reader=activity_reader,
        )
    assert events == ["clock:api.bybit.com"]
