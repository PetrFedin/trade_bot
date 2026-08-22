from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import pytest

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflight,
    BybitMainnetClockPreflightError,
)
from app.execution.bybit_mainnet_readonly import BybitMainnetReadOnlyError
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


def _execution(
    *,
    exec_id: str = "exec-1",
    exec_fee: str = "0.55",
    exec_type: str = "Trade",
) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "orderId": "order-1",
        "orderLinkId": "ASTRA-OBS-1",
        "side": "Buy",
        "orderType": "Market",
        "execType": exec_type,
        "execTime": str(_START + 10_000),
        "execPrice": "100000",
        "execQty": "0.01",
        "execValue": "1000",
        "execFee": exec_fee,
        "feeCurrency": "",
        "feeRate": "0.00055",
        "isMaker": False,
        "leavesQty": "0",
        "closedSize": "0",
        "execId": exec_id,
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
    funding: str = "-1",
    fee: str = "0.5",
    cash_flow: str = "5",
    change: str = "3.5",
) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "transactionTime": str(transaction_time_ms),
        "type": "TRADE",
        "transSubType": "",
        "category": "linear",
        "currency": "USDT",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "tradeId": trade_id,
        "orderId": order_id,
        "orderLinkId": "ASTRA-OBS-1",
        "qty": "0.01",
        "size": "0.01",
        "tradePrice": "100000",
        "funding": funding,
        "fee": fee,
        "cashFlow": cash_flow,
        "change": change,
        "cashBalance": "1003.5",
        "feeRate": "0.0005",
    }


def _client(transport: _Transport) -> BybitMainnetReadOnlyActivityClient:
    return BybitMainnetReadOnlyActivityClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: _END,
    )


def _window() -> BybitMainnetActivityWindow:
    return BybitMainnetActivityWindow(_START, _END)


def _clock(*, offset_ms: int = 0) -> BybitMainnetClockPreflight:
    preflight = BybitMainnetClockPreflight(
        api_host="api.bybit.com",
        local_send_time_ms=_END - 100,
        local_receive_time_ms=_END + 100,
        server_time_ms=_END + offset_ms,
        round_trip_time_ms=200,
        estimated_clock_offset_ms=offset_ms,
        uncertainty_ms=100,
        worst_case_abs_clock_skew_ms=abs(offset_ms) + 100,
    )
    preflight.validate()
    return preflight


def test_activity_window_is_positive_and_never_exceeds_seven_days() -> None:
    _window().validate()
    assert BybitMainnetActivityWindow.last_24_hours_ending_at(_END).duration_ms == 86_400_000
    with pytest.raises(ValueError, match="positive duration"):
        BybitMainnetActivityWindow(_START, _START).validate()
    with pytest.raises(ValueError, match="cannot exceed 7 days"):
        BybitMainnetActivityWindow(
            _START,
            _START + 7 * 86_400_000 + 1,
        ).validate()


def test_reader_uses_bounded_linear_usdt_queries_and_safe_totals() -> None:
    transport = _Transport(
        executions=[_execution(), _execution(exec_id="funding", exec_type="Funding")],
        closed_pnl=[_closed_pnl(), {"symbol": "BTCPERP"}],
        transactions=[_transaction()],
    )
    client = _client(transport)

    snapshot = read_bybit_mainnet_activity(client, window=_window())

    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert not hasattr(client, "place_order")
    assert len(snapshot.executions) == 1
    assert snapshot.excluded_non_trade_execution_count == 1
    assert len(snapshot.closed_pnl) == 1
    assert snapshot.excluded_non_usdt_closed_pnl_count == 1
    assert len(snapshot.transactions) == 1
    assert snapshot.transaction_cash_flow_usdt == Decimal("5")
    assert snapshot.transaction_funding_usdt == Decimal("-1")
    assert snapshot.transaction_fee_usdt == Decimal("0.5")
    assert snapshot.transaction_change_usdt == Decimal("3.5")

    assert [path for path, _ in transport.calls] == [
        "/v5/user/query-api",
        "/v5/execution/list",
        "/v5/position/closed-pnl",
        "/v5/account/transaction-log",
    ]
    assert transport.calls[1][1] == {
        "category": ["linear"],
        "settleCoin": ["USDT"],
        "startTime": [str(_START)],
        "endTime": [str(_END)],
        "limit": ["100"],
    }
    assert transport.calls[2][1] == {
        "category": ["linear"],
        "startTime": [str(_START)],
        "endTime": [str(_END)],
        "limit": ["100"],
    }
    assert transport.calls[3][1] == {
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


def test_blank_execution_fee_currency_remains_unknown_not_usdt() -> None:
    snapshot = read_bybit_mainnet_activity(
        _client(_Transport(executions=[_execution()])),
        window=_window(),
    )
    assert snapshot.executions[0].fee_currency is None
    safe = snapshot.to_safe_dict()
    assert safe["executions"][0]["fee_currency"] is None
    assert "execution_fee_usdt" not in safe["summary"]


def test_blank_funding_is_zero_but_missing_funding_fails_closed() -> None:
    blank = read_bybit_mainnet_activity(
        _client(_Transport(transactions=[_transaction(funding="", change="4.5")])),
        window=_window(),
    )
    assert blank.transactions[0].funding == Decimal("0")
    assert blank.transaction_change_usdt == Decimal("4.5")

    missing = _transaction()
    del missing["funding"]
    with pytest.raises(Exception, match="funding"):
        read_bybit_mainnet_activity(
            _client(_Transport(transactions=[missing])),
            window=_window(),
        )


def test_repeated_documented_transaction_id_is_not_used_as_sole_identity() -> None:
    first = _transaction(
        transaction_id="same-id",
        transaction_time_ms=_START + 30_000,
        trade_id="trade-1",
        order_id="order-1",
    )
    second = _transaction(
        transaction_id="same-id",
        transaction_time_ms=_START + 40_000,
        trade_id="trade-2",
        order_id="order-2",
    )
    snapshot = read_bybit_mainnet_activity(
        _client(_Transport(transactions=[second, first])),
        window=_window(),
    )
    assert len(snapshot.transactions) == 2
    assert [item.trade_id for item in snapshot.transactions] == ["trade-1", "trade-2"]


def test_same_composite_transaction_identity_conflict_fails_closed() -> None:
    first = _transaction()
    second = dict(first)
    second["fee"] = "0.7"
    second["change"] = "3.3"
    with pytest.raises(BybitMainnetActivityError, match="conflicting broker records"):
        read_bybit_mainnet_activity(
            _client(_Transport(transactions=[first, second])),
            window=_window(),
        )


def test_transaction_accounting_identity_fails_closed_on_bad_broker_row() -> None:
    with pytest.raises(BybitMainnetActivityError, match="change = cashFlow"):
        read_bybit_mainnet_activity(
            _client(_Transport(transactions=[_transaction(change="99")])),
            window=_window(),
        )


def test_snapshot_rejects_host_outside_qualified_allowlist() -> None:
    snapshot = BybitMainnetActivitySnapshot(
        window=_window(),
        api_host="evil.example",
        api_key_fingerprint_sha256="a" * 64,
        executions=(),
        closed_pnl=(),
        transactions=(),
        excluded_non_trade_execution_count=0,
        excluded_non_usdt_closed_pnl_count=0,
        transaction_cash_flow_usdt=Decimal("0"),
        transaction_funding_usdt=Decimal("0"),
        transaction_fee_usdt=Decimal("0"),
        transaction_change_usdt=Decimal("0"),
    )
    with pytest.raises(BybitMainnetReadOnlyError, match="regional allowlist"):
        snapshot.validate()


def test_activity_probe_anchors_last_24h_to_bybit_server_time() -> None:
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
            excluded_non_trade_execution_count=0,
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


def test_activity_probe_never_reads_private_activity_when_clock_is_unsafe() -> None:
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
