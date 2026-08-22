from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflight,
    BybitMainnetClockPreflightError,
)
from app.execution.bybit_mainnet_readonly_activity import (
    BybitMainnetActivityError,
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


class _FakeTransport:
    def __init__(self, responses: list[tuple[str, Mapping[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        expected_path, response = self.responses.pop(0)
        assert path == expected_path
        assert headers["X-BAPI-API-KEY"] == "key"
        self.calls.append((path, query_string))
        return response


def _ok(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": dict(result)}


def _key_result() -> dict[str, Any]:
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


def _execution(
    *,
    exec_id: str,
    order_id: str,
    fee: str,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "orderId": order_id,
        "orderLinkId": "ASTRA-OBS-1",
        "side": "Buy",
        "orderType": "Market",
        "leavesQty": "0",
        "execFee": fee,
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
        "seq": 101,
    }


def _closed_pnl(*, symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "orderId": "close-1",
        "side": "Sell",
        "qty": "0.01",
        "orderPrice": "99000",
        "orderType": "Market",
        "execType": "Trade",
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
    transaction_id: str,
    cash_flow: str,
    funding: str,
    fee: str,
    change: str,
    time_offset_ms: int,
) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "symbol": "BTCUSDT",
        "category": "linear",
        "side": "None",
        "transactionTime": str(_START + time_offset_ms),
        "type": "SETTLEMENT",
        "transSubType": "",
        "qty": "",
        "size": "-0.01",
        "currency": "USDT",
        "tradePrice": "",
        "funding": funding,
        "fee": fee,
        "cashFlow": cash_flow,
        "change": change,
        "cashBalance": "1005.6",
        "feeRate": "",
        "tradeId": "",
        "orderId": "",
        "orderLinkId": "",
    }


def _activity_transport(
    *,
    executions: list[Mapping[str, Any]] | None = None,
    transactions: list[Mapping[str, Any]] | None = None,
) -> _FakeTransport:
    execution_rows = (
        [
            _execution(exec_id="exec-b", order_id="order-b", fee="0.55"),
            _execution(exec_id="exec-a", order_id="order-a", fee="0.55"),
        ]
        if executions is None
        else executions
    )
    transaction_rows = (
        [
            _transaction(
                transaction_id="tx-b",
                cash_flow="0",
                funding="2",
                fee="-0.1",
                change="2.1",
                time_offset_ms=40_000,
            ),
            _transaction(
                transaction_id="tx-a",
                cash_flow="5",
                funding="-1",
                fee="0.5",
                change="3.5",
                time_offset_ms=30_000,
            ),
        ]
        if transactions is None
        else transactions
    )
    return _FakeTransport(
        [
            ("/v5/user/query-api", _key_result()),
            (
                "/v5/execution/list",
                _ok({"list": execution_rows, "nextPageCursor": ""}),
            ),
            (
                "/v5/position/closed-pnl",
                _ok(
                    {
                        "list": [
                            _closed_pnl(),
                            {"symbol": "ETHPERP"},
                        ],
                        "nextPageCursor": "",
                    }
                ),
            ),
            (
                "/v5/account/transaction-log",
                _ok({"list": transaction_rows, "nextPageCursor": ""}),
            ),
        ]
    )


def _client(transport: _FakeTransport) -> BybitMainnetReadOnlyActivityClient:
    return BybitMainnetReadOnlyActivityClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: _END,
    )


def _window() -> BybitMainnetActivityWindow:
    return BybitMainnetActivityWindow(start_time_ms=_START, end_time_ms=_END)


def _clock(*, ready: bool = True) -> BybitMainnetClockPreflight:
    offset = 0 if ready else 600
    preflight = BybitMainnetClockPreflight(
        api_host="api.bybit.com",
        local_send_time_ms=_END - 100,
        local_receive_time_ms=_END + 100,
        server_time_ms=_END + offset,
        round_trip_time_ms=200,
        estimated_clock_offset_ms=offset,
        uncertainty_ms=100,
        worst_case_abs_clock_skew_ms=abs(offset) + 100,
    )
    preflight.validate()
    return preflight


def test_activity_window_is_positive_and_at_most_seven_days() -> None:
    _window().validate()
    last_day = BybitMainnetActivityWindow.last_24_hours_ending_at(_END)
    assert last_day.duration_ms == 86_400_000

    with pytest.raises(ValueError, match="positive duration"):
        BybitMainnetActivityWindow(_START, _START).validate()
    with pytest.raises(ValueError, match="cannot exceed 7 days"):
        BybitMainnetActivityWindow(
            _START,
            _START + 7 * 86_400_000 + 1,
        ).validate()


def test_activity_reader_uses_bounded_usdt_queries_and_deterministic_ordering() -> None:
    transport = _activity_transport()
    client = _client(transport)

    snapshot = read_bybit_mainnet_activity(client, window=_window())

    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert not hasattr(client, "place_order")
    assert [record.exec_id for record in snapshot.executions] == ["exec-a", "exec-b"]
    assert [record.transaction_id for record in snapshot.transactions] == ["tx-a", "tx-b"]
    assert len(snapshot.closed_pnl) == 1
    assert snapshot.closed_pnl[0].symbol == "BTCUSDT"
    assert snapshot.excluded_non_usdt_closed_pnl_count == 1
    assert snapshot.transaction_cash_flow_usdt == Decimal("5")
    assert snapshot.transaction_funding_usdt == Decimal("1")
    assert snapshot.transaction_fee_usdt == Decimal("0.4")
    assert snapshot.transaction_change_usdt == Decimal("5.6")
    assert snapshot.transaction_change_usdt == (
        snapshot.transaction_cash_flow_usdt
        + snapshot.transaction_funding_usdt
        - snapshot.transaction_fee_usdt
    )
    assert transport.calls == [
        ("/v5/user/query-api", ""),
        (
            "/v5/execution/list",
            f"category=linear&endTime={_END}&limit=100&settleCoin=USDT&startTime={_START}",
        ),
        (
            "/v5/position/closed-pnl",
            f"category=linear&endTime={_END}&limit=100&startTime={_START}",
        ),
        (
            "/v5/account/transaction-log",
            "accountType=UNIFIED&category=linear&currency=USDT"
            f"&endTime={_END}&limit=50&startTime={_START}",
        ),
    ]


def test_execution_duplicate_is_idempotent_but_conflict_fails_closed() -> None:
    duplicate = _execution(exec_id="same", order_id="order-1", fee="0.5")
    snapshot = read_bybit_mainnet_activity(
        _client(_activity_transport(executions=[duplicate, dict(duplicate)])),
        window=_window(),
    )
    assert len(snapshot.executions) == 1

    conflicting = dict(duplicate)
    conflicting["execFee"] = "0.7"
    with pytest.raises(BybitMainnetActivityError, match="conflicting broker records"):
        read_bybit_mainnet_activity(
            _client(_activity_transport(executions=[duplicate, conflicting])),
            window=_window(),
        )


def test_transaction_accounting_identity_fails_closed_on_broker_inconsistency() -> None:
    bad = _transaction(
        transaction_id="tx-bad",
        cash_flow="5",
        funding="-1",
        fee="0.5",
        change="99",
        time_offset_ms=30_000,
    )
    with pytest.raises(BybitMainnetActivityError, match="change = cashFlow"):
        read_bybit_mainnet_activity(
            _client(_activity_transport(transactions=[bad])),
            window=_window(),
        )


def test_empty_execution_fee_currency_remains_unknown_not_assumed_usdt() -> None:
    snapshot = read_bybit_mainnet_activity(
        _client(_activity_transport()),
        window=_window(),
    )

    assert all(record.fee_currency is None for record in snapshot.executions)
    safe = snapshot.to_safe_dict()
    assert "execution_fee_usdt" not in safe["summary"]
    assert safe["executions"][0]["fee_currency"] is None


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
    template = read_bybit_mainnet_activity(
        _client(_activity_transport()),
        window=_window(),
    )

    def clock_probe(*, host: str) -> BybitMainnetClockPreflight:
        events.append(f"clock:{host}")
        return preflight

    def activity_reader(
        client: BybitMainnetReadOnlyActivityClient,
        *,
        window: BybitMainnetActivityWindow,
    ) -> Any:
        events.append("activity")
        assert client.host == "api.bybit.com"
        assert client.live_mainnet_order_routing_allowed is False
        assert window == expected_window
        return template.__class__(
            window=window,
            api_host=client.host,
            api_key_fingerprint_sha256=template.api_key_fingerprint_sha256,
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


def test_activity_probe_never_reads_private_activity_when_clock_is_unsafe() -> None:
    events: list[str] = []
    credentials = BybitMainnetReadOnlyCredentials(
        api_key="key",
        api_secret="secret",
        site="global",
    )

    def clock_probe(*, host: str) -> BybitMainnetClockPreflight:
        events.append(f"clock:{host}")
        return _clock(ready=False)

    def activity_reader(*args: Any, **kwargs: Any) -> Any:
        events.append("activity")
        raise AssertionError("private activity reader must not run")

    with pytest.raises(BybitMainnetClockPreflightError, match="CLOCK_SKEW_UNSAFE"):
        probe_bybit_mainnet_readonly_activity(
            credentials=credentials,
            clock_probe=clock_probe,
            activity_reader=activity_reader,
        )

    assert events == ["clock:api.bybit.com"]
