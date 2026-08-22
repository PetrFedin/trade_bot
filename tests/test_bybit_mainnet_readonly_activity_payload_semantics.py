from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_mainnet_readonly import BybitMainnetReadOnlyError
from app.execution.bybit_mainnet_readonly_activity import (
    BybitMainnetActivityError,
    BybitMainnetActivitySnapshot,
    BybitMainnetActivityWindow,
    BybitMainnetReadOnlyActivityClient,
    read_bybit_mainnet_activity,
)

_START = 1_787_000_000_000
_END = _START + 60_000


class _Transport:
    def __init__(self, transactions: list[Mapping[str, Any]]) -> None:
        self.transactions = transactions
        self.calls = 0

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        del query_string
        assert headers["X-BAPI-API-KEY"] == "key"
        self.calls += 1
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
            return _ok({"list": [], "nextPageCursor": ""})
        if path == "/v5/position/closed-pnl":
            return _ok({"list": [], "nextPageCursor": ""})
        if path == "/v5/account/transaction-log":
            return _ok({"list": self.transactions, "nextPageCursor": ""})
        raise AssertionError(f"unexpected read path {path}")


def _ok(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": dict(result)}


def _transaction(
    *,
    transaction_id: str,
    time_ms: int,
    trade_id: str,
    order_id: str,
    funding: str = "",
    fee: str = "0.5",
    cash_flow: str = "0",
    change: str = "-0.5",
) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "symbol": "BTCUSDT",
        "category": "linear",
        "side": "Buy",
        "transactionTime": str(time_ms),
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
        "cashBalance": "999.5",
        "feeRate": "0.0005",
        "tradeId": trade_id,
        "orderId": order_id,
        "orderLinkId": "ASTRA-OBS-1",
    }


def _read(transactions: list[Mapping[str, Any]]) -> BybitMainnetActivitySnapshot:
    client = BybitMainnetReadOnlyActivityClient(
        api_key="key",
        api_secret="secret",
        transport=_Transport(transactions),
        clock_ms=lambda: _END,
    )
    return read_bybit_mainnet_activity(
        client,
        window=BybitMainnetActivityWindow(_START, _END),
    )


def test_blank_funding_field_is_zero_component_not_protocol_failure() -> None:
    snapshot = _read(
        [
            _transaction(
                transaction_id="tx-1",
                time_ms=_START + 10_000,
                trade_id="trade-1",
                order_id="order-1",
                funding="",
                fee="0.5",
                cash_flow="0",
                change="-0.5",
            )
        ]
    )

    assert snapshot.transactions[0].funding == Decimal("0")
    assert snapshot.transaction_funding_usdt == Decimal("0")
    assert snapshot.transaction_fee_usdt == Decimal("0.5")
    assert snapshot.transaction_change_usdt == Decimal("-0.5")


def test_repeated_documented_id_value_at_different_broker_identity_is_not_collapsed() -> None:
    snapshot = _read(
        [
            _transaction(
                transaction_id="same-id",
                time_ms=_START + 10_000,
                trade_id="trade-1",
                order_id="order-1",
            ),
            _transaction(
                transaction_id="same-id",
                time_ms=_START + 20_000,
                trade_id="trade-2",
                order_id="order-2",
            ),
        ]
    )

    assert len(snapshot.transactions) == 2
    assert [row.trade_id for row in snapshot.transactions] == ["trade-1", "trade-2"]


def test_same_composite_transaction_identity_with_conflicting_economics_fails_closed() -> None:
    first = _transaction(
        transaction_id="same-id",
        time_ms=_START + 10_000,
        trade_id="trade-1",
        order_id="order-1",
    )
    conflicting = dict(first)
    conflicting["fee"] = "0.7"
    conflicting["change"] = "-0.7"

    with pytest.raises(BybitMainnetActivityError, match="conflicting broker records"):
        _read([first, conflicting])


def test_snapshot_rejects_api_host_outside_qualified_allowlist() -> None:
    snapshot = BybitMainnetActivitySnapshot(
        window=BybitMainnetActivityWindow(_START, _END),
        api_host="evil.example",
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

    with pytest.raises(BybitMainnetReadOnlyError, match="regional allowlist"):
        snapshot.validate()
