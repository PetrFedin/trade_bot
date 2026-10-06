from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_instruments import parse_response
from app.marketdata.bybit_public import BybitPublicLinearSubscription
from app.marketdata.bybit_repair import BybitKlineRangeCapture
from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    continuity_checkpoint_id,
)
from app.marketdata.operational import OperationalBar
from app.qualification.adapter_conformance import AdapterConformanceStatus
from app.qualification.bybit_adapter_conformance import (
    BybitPublicAdapterSubject,
    qualify_bybit_public_marketdata_adapter,
)
from app.qualification.bybit_provider_replay import (
    build_bybit_provider_complete_replay,
)

BASE = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
RECEIVED = BASE + timedelta(minutes=15, seconds=2)
STRATEGY = "bybit-demo-momentum-v1"
REQUEST_URL = (
    "https://api.bybit.com/v5/market/kline?"
    "category=linear&symbol=BTCUSDT&interval=5&start=1791270000000&end=1791270899999&limit=3"
)


def subscription() -> BybitPublicLinearSubscription:
    return BybitPublicLinearSubscription(
        symbol="BTCUSDT",
        interval="5",
        strategy_id=STRATEGY,
    )


def raw_row(index: int) -> tuple[str, ...]:
    start = BASE + timedelta(minutes=5 * index)
    price = Decimal(str(100 + index))
    return (
        str(int(start.timestamp() * 1000)),
        str(price),
        str(price + Decimal("1")),
        str(price - Decimal("1")),
        str(price),
        "10",
        "1000",
    )


def operational_bar(index: int) -> OperationalBar:
    row = raw_row(index)
    open_time = BASE + timedelta(minutes=5 * index)
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=5),
        source_timestamp=open_time + timedelta(minutes=5) - timedelta(milliseconds=1),
        received_at=RECEIVED,
        source_event_id=f"kline.5.BTCUSDT:{row[0]}:{int(row[0]) + 300_000 - 1}",
        is_final=True,
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        volume=Decimal(row[5]),
    )


def capture() -> BybitKlineRangeCapture:
    rows = tuple(raw_row(index) for index in range(3))
    bars = tuple(operational_bar(index) for index in range(3))
    body = json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "symbol": "BTCUSDT",
                "category": "linear",
                "list": [list(row) for row in reversed(rows)],
            },
            "retExtInfo": {},
            "time": int(RECEIVED.timestamp() * 1000),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    value = BybitKlineRangeCapture(
        request_url=REQUEST_URL,
        response_status=200,
        response_body=body,
        response_received_at=RECEIVED,
        server_at=RECEIVED,
        raw_rows=rows,
        bars=bars,
    )
    value.validate()
    return value


def instrument_spec():
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "priceFilter": {
                        "minPrice": "0.10",
                        "maxPrice": "1999999.80",
                        "tickSize": "0.10",
                    },
                    "lotSizeFilter": {
                        "maxOrderQty": "1500.000",
                        "minOrderQty": "0.001",
                        "qtyStep": "0.001",
                        "maxMktOrderQty": "150.000",
                        "minNotionalValue": "5",
                    },
                    "leverageFilter": {
                        "minLeverage": "1",
                        "maxLeverage": "150.00",
                        "leverageStep": "0.01",
                    },
                }
            ]
        },
        "time": int(RECEIVED.timestamp() * 1000),
    }
    return parse_response(
        payload,
        category="linear",
        environment="mainnet",
        observed_at=RECEIVED,
    )[0]


def checkpoint(cap: BybitKlineRangeCapture) -> OperationalContinuityCheckpoint:
    tail = cap.bars[-1]
    checkpoint_id = continuity_checkpoint_id(
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=tail.bar_id,
        through_close_time=tail.close_time,
        evidence_source="BYBIT_V5_MARKET_KLINE_GET",
    )
    value = OperationalContinuityCheckpoint(
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=tail.bar_id,
        through_close_time=tail.close_time,
        established_at=RECEIVED,
        evidence_source="BYBIT_V5_MARKET_KLINE_GET",
    )
    value.validate()
    return value


def artifacts():
    cap = capture()
    spec = instrument_spec()
    proof = checkpoint(cap)
    replay = build_bybit_provider_complete_replay(
        capture=cap,
        instrument_spec=spec,
        continuity_checkpoint=proof,
        strategy_id=STRATEGY,
    )
    return cap, spec, proof, replay


def test_bybit_public_marketdata_adapter_qualifies_with_machine_readable_evidence() -> None:
    cap, spec, proof, replay = artifacts()

    result = qualify_bybit_public_marketdata_adapter(
        subject=BybitPublicAdapterSubject(adapter_version="7.39.0"),
        subscription=subscription(),
        instrument_spec=spec,
        capture=cap,
        continuity_checkpoint=proof,
        replay_evidence=replay,
    )

    assert result.qualified
    assert result.status is AdapterConformanceStatus.QUALIFIED
    assert result.profile_id == "ASTRA_BYBIT_PUBLIC_MARKETDATA"
    assert result.profile_version == "1.0.0"
    assert result.scope == "PUBLIC_MARKET_DATA_ONLY"
    assert len(result.checks) == 8
    assert all(check.passed for check in result.checks)
    assert result.limitations == (
        "NO_ORDER_SUBMIT_CONFORMANCE",
        "NO_ORDER_STATUS_CONFORMANCE",
        "NO_FILL_MAPPING_CONFORMANCE",
        "NO_CANCEL_REPLACE_CONFORMANCE",
        "NO_PRIVATE_ACCOUNT_CONFORMANCE",
    )
    assert len(result.result_sha256) == 64


def test_conformance_result_is_deterministic_for_identical_evidence() -> None:
    cap, spec, proof, replay = artifacts()
    kwargs = dict(
        subject=BybitPublicAdapterSubject(adapter_version="7.39.0"),
        subscription=subscription(),
        instrument_spec=spec,
        capture=cap,
        continuity_checkpoint=proof,
        replay_evidence=replay,
    )

    first = qualify_bybit_public_marketdata_adapter(**kwargs)
    second = qualify_bybit_public_marketdata_adapter(**kwargs)

    assert first == second
    assert first.result_sha256 == second.result_sha256


def test_environment_mismatch_rejects_profile_instead_of_overclaiming_scope() -> None:
    cap, spec, proof, replay = artifacts()

    result = qualify_bybit_public_marketdata_adapter(
        subject=BybitPublicAdapterSubject(
            adapter_version="7.39.0",
            environment="testnet",
        ),
        subscription=subscription(),
        instrument_spec=spec,
        capture=cap,
        continuity_checkpoint=proof,
        replay_evidence=replay,
    )

    assert not result.qualified
    assert result.status is AdapterConformanceStatus.REJECTED
    failed = [check for check in result.checks if not check.passed]
    assert [check.check_id for check in failed] == ["BYBIT-PMD-002-INSTRUMENT-SPEC"]
    assert failed[0].reason == (
        "instrument specification is not a tradable matching Bybit linear spec"
    )


def test_instrument_not_trading_rejects_conformance() -> None:
    cap, spec, proof, replay = artifacts()
    non_trading = replace(spec, status=type(spec.status).CLOSED)

    result = qualify_bybit_public_marketdata_adapter(
        subject=BybitPublicAdapterSubject(adapter_version="7.39.0"),
        subscription=subscription(),
        instrument_spec=non_trading,
        capture=cap,
        continuity_checkpoint=proof,
        replay_evidence=replay,
    )

    assert not result.qualified
    failed = {check.check_id for check in result.checks if not check.passed}
    assert "BYBIT-PMD-002-INSTRUMENT-SPEC" in failed


def test_profile_never_claims_private_or_order_adapter_conformance() -> None:
    cap, spec, proof, replay = artifacts()

    result = qualify_bybit_public_marketdata_adapter(
        subject=BybitPublicAdapterSubject(adapter_version="7.39.0"),
        subscription=subscription(),
        instrument_spec=spec,
        capture=cap,
        continuity_checkpoint=proof,
        replay_evidence=replay,
    )

    assert result.scope == "PUBLIC_MARKET_DATA_ONLY"
    assert all("ORDER" in limitation or "PRIVATE" in limitation or "FILL" in limitation
               for limitation in result.limitations)
