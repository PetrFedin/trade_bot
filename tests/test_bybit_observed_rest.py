from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoHttpJson
from app.execution.bybit_observed_rest import (
    ObservedBybitDemoAccountingClient,
    ObservedBybitDemoBrokerTruthClient,
    ObservedBybitDemoStopRatchetClient,
)
from app.observability.bybit_runtime_health import BybitRestHealthRecorder


class _AccountingTransport:
    def get(self, *, path, query_string, headers):
        assert path == "/v5/account/wallet-balance"
        assert query_string == "accountType=UNIFIED"
        assert "X-BAPI-SIGN" in headers
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": "1000",
                        "totalWalletBalance": "990",
                        "totalMarginBalance": "995",
                        "totalAvailableBalance": "900",
                        "totalPerpUPL": "5",
                        "totalInitialMargin": "50",
                        "totalMaintenanceMargin": "10",
                    }
                ]
            },
        }


class _FailingHealthSink:
    def record(self, **_kwargs) -> None:
        raise RuntimeError("telemetry sink unavailable")


def _clock(*values: float):
    iterator = iter(values)
    return lambda: next(iterator)


def test_observed_trade_read_records_logical_success_latency() -> None:
    health = BybitRestHealthRecorder()

    def transport(method, url, headers, body):
        assert method == "GET"
        assert body is None
        assert "api-demo.bybit.com" in url
        assert "X-BAPI-SIGN" in headers
        return BybitDemoHttpJson(
            200,
            {},
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "takerFeeRate": "0.00055",
                            "makerFeeRate": "0.0002",
                        }
                    ]
                },
            },
        )

    client = ObservedBybitDemoStopRatchetClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1_800_000_000_000,
        rest_health_sink=health,
        monotonic_fn=_clock(10.0, 11.0),
    )

    fee = client.get_fee_rate(symbol="BTCUSDT")
    snapshot = health.snapshot()

    assert fee.taker_fee_rate == Decimal("0.00055")
    assert snapshot.total_calls == 1
    assert snapshot.window_errors == 0
    assert snapshot.last_latency_ms == Decimal("1000.0")
    assert client.rest_health_recording_error_type is None


def test_observed_broker_truth_records_terminal_read_failure() -> None:
    health = BybitRestHealthRecorder()

    def transport(_method, _url, _headers, _body):
        raise OSError("network down")

    client = ObservedBybitDemoBrokerTruthClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1_800_000_000_000,
        sleep_fn=lambda _delay: None,
        rest_health_sink=health,
        monotonic_fn=_clock(20.0, 21.0),
    )

    with pytest.raises(RuntimeError, match="bounded retries"):
        client.get_open_orders()

    snapshot = health.snapshot()
    assert snapshot.total_calls == 1
    assert snapshot.window_errors == 1
    assert snapshot.error_fraction == Decimal("1")
    assert snapshot.last_error_type is not None


def test_observed_accounting_uses_same_health_recorder_contract() -> None:
    health = BybitRestHealthRecorder()
    client = ObservedBybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=_AccountingTransport(),
        clock_ms=lambda: 1_800_000_000_000,
        rest_health_sink=health,
        monotonic_fn=_clock(30.0, 30.25),
    )

    wallet = client.get_wallet_balance()
    snapshot = health.snapshot()

    assert wallet.total_equity_usd == Decimal("1000")
    assert snapshot.total_calls == 1
    assert snapshot.window_errors == 0
    assert snapshot.last_latency_ms == Decimal("250.00")


def test_health_sink_failure_never_replaces_successful_broker_result() -> None:
    def transport(_method, _url, _headers, _body):
        return BybitDemoHttpJson(
            200,
            {},
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "takerFeeRate": "0.00055",
                            "makerFeeRate": "0.0002",
                        }
                    ]
                },
            },
        )

    client = ObservedBybitDemoStopRatchetClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1_800_000_000_000,
        rest_health_sink=_FailingHealthSink(),
        monotonic_fn=_clock(40.0, 41.0),
    )

    fee = client.get_fee_rate(symbol="BTCUSDT")

    assert fee.taker_fee_rate == Decimal("0.00055")
    assert client.rest_health_recording_error_type == "RuntimeError"
