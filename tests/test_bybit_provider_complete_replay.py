from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_instruments import parse_response
from app.marketdata.bybit_public import BybitPublicLinearSubscription
from app.marketdata.bybit_repair import (
    BybitContinuityRepairService,
    BybitPublicKlineClient,
    HttpResponse,
)
from app.marketdata.continuity import (
    SQLiteOperationalContinuityStore,
    SQLiteOperationalRepairBarStore,
)
from app.marketdata.operational import SQLiteOperationalMarketDataStore
from app.qualification.bybit_provider_replay import (
    build_bybit_provider_complete_replay,
)
from app.qualification.replay_evidence import (
    ReplayContinuity,
    ReplayEvidenceProfile,
)

BASE = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
OBSERVED = BASE + timedelta(minutes=15, seconds=2)
STRATEGY = "bybit-demo-momentum-v1"


def subscription() -> BybitPublicLinearSubscription:
    return BybitPublicLinearSubscription(
        symbol="BTCUSDT",
        interval="5",
        strategy_id=STRATEGY,
    )


def row(index: int, *, close: str | None = None) -> list[str]:
    start = BASE + timedelta(minutes=5 * index)
    price = Decimal(close if close is not None else str(100 + index))
    return [
        str(int(start.timestamp() * 1000)),
        str(price),
        str(price + Decimal("1")),
        str(price - Decimal("1")),
        str(price),
        "10",
        "1000",
    ]


def response_body(rows: list[list[str]]) -> bytes:
    return json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "symbol": "BTCUSDT",
                "category": "linear",
                "list": list(reversed(rows)),
            },
            "retExtInfo": {},
            "time": int(OBSERVED.timestamp() * 1000),
        },
        separators=(",", ":"),
    ).encode("utf-8")


INSTRUMENT = {
    "symbol": "BTCUSDT",
    "status": "Trading",
    "baseCoin": "BTC",
    "quoteCoin": "USDT",
    "settleCoin": "USDT",
    "contractType": "LinearPerpetual",
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


class FakeTransport:
    def __init__(self, body: bytes, *, received_at: datetime = OBSERVED) -> None:
        self.body = body
        self.received_at = received_at
        self.urls: list[str] = []

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> HttpResponse:
        assert timeout_seconds > 0
        assert len(self.body) <= maximum_response_bytes
        self.urls.append(url)
        return HttpResponse(
            status=200,
            body=self.body,
            received_at=self.received_at,
        )


def instrument_spec():
    envelope = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": [INSTRUMENT]},
        "time": int(OBSERVED.timestamp() * 1000),
    }
    return parse_response(
        envelope,
        category="linear",
        observed_at=OBSERVED,
    )[0]


def test_continuity_repair_retains_exact_provider_capture_and_builds_complete_replay(
    tmp_path,
) -> None:
    body = response_body([row(0), row(1), row(2)])
    transport = FakeTransport(body)
    path = tmp_path / "marketdata.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(path)
    repair_store = SQLiteOperationalRepairBarStore(path)
    continuity = SQLiteOperationalContinuityStore(path)
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
    )
    service = BybitContinuityRepairService(
        subscription=subscription(),
        marketdata=marketdata,
        repair_store=repair_store,
        continuity=continuity,
        client=client,
    )

    result = service.repair(
        observed_at=OBSERVED,
        bootstrap_open_time=BASE,
    )

    capture = result.provider_capture
    assert capture is not None
    assert capture.response_body == body
    assert capture.response_sha256 == hashlib.sha256(body).hexdigest()
    assert capture.response_received_at == OBSERVED
    assert capture.server_at == OBSERVED
    assert len(capture.raw_rows) == 3
    assert len(set(capture.raw_row_sha256)) == 3
    assert capture.bars[-1].bar_id == result.checkpoint.through_bar_id

    evidence = build_bybit_provider_complete_replay(
        capture=capture,
        instrument_spec=instrument_spec(),
        continuity_checkpoint=result.checkpoint,
        strategy_id=STRATEGY,
    )

    assert evidence.replay.profile is ReplayEvidenceProfile.PROVIDER_COMPLETE
    assert evidence.replay.limitations == ()
    assert evidence.raw_response_sha256 == hashlib.sha256(body).hexdigest()
    assert evidence.continuity_checkpoint_id == result.checkpoint.checkpoint_id
    assert evidence.continuity_through_bar_id == capture.bars[-1].bar_id
    assert [event.continuity for event in evidence.replay.events] == [
        ReplayContinuity.ROOT,
        ReplayContinuity.CONTIGUOUS,
        ReplayContinuity.CONTIGUOUS,
    ]
    for index, (event, binding, bar) in enumerate(
        zip(evidence.replay.events, evidence.bindings, capture.bars, strict=True)
    ):
        assert event.sequence == index + 1
        assert event.source_event_id == bar.source_event_id
        assert event.receive_timestamp == OBSERVED
        assert event.exchange_timestamp == bar.source_timestamp
        assert event.raw_event_sha256 == capture.raw_row_sha256[index]
        assert event.normalized_event_sha256 == bar.content_hash
        assert binding.normalized_event_sha256 == bar.content_hash
        assert len(binding.strategy_input_sha256) == 64
        assert len(binding.risk_market_input_sha256) == 64
        assert binding.replay_event_digest == event.digest
    assert len(evidence.replay.evidence_sha256) == 64
    assert len(evidence.evidence_sha256) == 64


def test_capture_rejects_raw_response_substitution_even_when_normalized_bars_are_unchanged(
    tmp_path,
) -> None:
    original_body = response_body([row(0), row(1), row(2)])
    transport = FakeTransport(original_body)
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
    )
    capture = client.fetch_closed_range_capture(
        first_open_time=BASE,
        last_open_time=BASE + timedelta(minutes=10),
        observed_at=OBSERVED,
    )

    substituted = replace(
        capture,
        response_body=response_body([row(0), row(1, close="777"), row(2)]),
    )
    with pytest.raises(ValueError, match="raw rows disagree"):
        substituted.validate()


def test_transport_receive_timestamp_is_preserved_in_provider_evidence() -> None:
    received = OBSERVED + timedelta(milliseconds=250)
    transport = FakeTransport(
        response_body([row(0), row(1), row(2)]),
        received_at=received,
    )
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
    )

    capture = client.fetch_closed_range_capture(
        first_open_time=BASE,
        last_open_time=BASE + timedelta(minutes=10),
        observed_at=OBSERVED,
    )

    assert capture.response_received_at == received
    assert all(bar.received_at == received for bar in capture.bars)
