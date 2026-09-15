from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.marketdata.bybit_public import BybitPublicLinearSubscription
from app.marketdata.bybit_repair import (
    BybitContinuityRepairService,
    BybitPublicKlineClient,
    BybitRepairError,
    BybitRepairPolicy,
    BybitRepairProtocolError,
    HttpResponse,
    StdlibBybitPublicHttpTransport,
)
from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    SQLiteOperationalContinuityStore,
    SQLiteOperationalRepairBarStore,
    continuity_checkpoint_id,
)
from app.marketdata.operational import OperationalBar, SQLiteOperationalMarketDataStore

BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
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


def payload(rows: list[list[str]], *, server_at: datetime = OBSERVED) -> bytes:
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
            "time": int(server_at.timestamp() * 1000),
        },
        separators=(",", ":"),
    ).encode()


class FakeTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> HttpResponse:
        assert timeout_seconds > 0
        assert maximum_response_bytes >= len(self.body)
        self.urls.append(url)
        return HttpResponse(status=200, body=self.body)


def stores(tmp_path):
    path = tmp_path / "marketdata.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(path)
    repair = SQLiteOperationalRepairBarStore(path)
    continuity = SQLiteOperationalContinuityStore(path)
    return path, marketdata, repair, continuity


def service(tmp_path, body: bytes, *, policy: BybitRepairPolicy | None = None):
    path, marketdata, repair, continuity = stores(tmp_path)
    transport = FakeTransport(body)
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
        policy=policy,
    )
    value = BybitContinuityRepairService(
        subscription=subscription(),
        marketdata=marketdata,
        repair_store=repair,
        continuity=continuity,
        client=client,
    )
    return path, marketdata, repair, continuity, transport, client, value


def economics_bar(index: int, *, received_at: datetime = OBSERVED) -> OperationalBar:
    raw = row(index)
    open_time = BASE + timedelta(minutes=5 * index)
    close_time = open_time + timedelta(minutes=5)
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time - timedelta(seconds=2),
        received_at=received_at,
        source_event_id=(
            f"kline.5.BTCUSDT:{raw[0]}:{int(raw[0]) + 300_000 - 1}"
        ),
        is_final=True,
        open=Decimal(raw[1]),
        high=Decimal(raw[2]),
        low=Decimal(raw[3]),
        close=Decimal(raw[4]),
        volume=Decimal(raw[5]),
        revision=0,
    )


def test_bootstrap_repairs_three_bars_without_creating_decision_tickets(tmp_path) -> None:
    _, marketdata, _, continuity, transport, _, value = service(
        tmp_path,
        payload([row(0), row(1), row(2)]),
    )

    result = value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)

    assert result.expected_bars == 3
    assert result.repaired_bars == 3
    assert result.existing_bars == 0
    assert marketdata.pending_decisions() == ()
    restored = marketdata.recent_bars(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_close_time=BASE + timedelta(minutes=15),
        limit=3,
    )
    assert [bar.close for bar in restored] == [
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
    ]
    latest = continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    )
    assert latest == result.checkpoint
    assert latest is not None and latest.through_close_time == BASE + timedelta(minutes=15)
    query = parse_qs(urlsplit(transport.urls[0]).query)
    assert query["category"] == ["linear"]
    assert query["limit"] == ["3"]


def test_restart_after_repair_bars_before_checkpoint_is_idempotent(tmp_path) -> None:
    _, marketdata, repair, continuity, _, client, value = service(
        tmp_path,
        payload([row(0), row(1), row(2)]),
    )
    fetched = client.fetch_closed_range(
        first_open_time=BASE,
        last_open_time=BASE + timedelta(minutes=10),
        observed_at=OBSERVED,
    )
    for bar in fetched:
        assert repair.record_without_decision(bar, recorded_at=OBSERVED)
    assert continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) is None

    result = value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)

    assert result.repaired_bars == 0
    assert result.existing_bars == 3
    assert marketdata.pending_decisions() == ()
    assert continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) == result.checkpoint


def test_existing_live_overlap_is_verified_without_duplicate_old_decisions(tmp_path) -> None:
    _, marketdata, _, _, _, _, value = service(
        tmp_path,
        payload([row(0), row(1), row(2)]),
    )
    live = economics_bar(0)
    original_ticket = marketdata.record_finalized_for_strategy(
        live,
        strategy_id=STRATEGY,
        recorded_at=OBSERVED,
    )

    result = value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)

    assert result.repaired_bars == 2
    assert result.existing_bars == 1
    assert marketdata.pending_decisions(strategy_id=STRATEGY) == (original_ticket,)


def test_missing_middle_rest_bar_fails_before_persistence_or_checkpoint(tmp_path) -> None:
    _, marketdata, _, continuity, _, _, value = service(
        tmp_path,
        payload([row(0), row(2)]),
    )

    with pytest.raises(BybitRepairProtocolError, match="NOT_CONTIGUOUS"):
        value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)

    assert marketdata.pending_decisions() == ()
    assert marketdata.recent_bars(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_close_time=OBSERVED,
        limit=10,
    ) == ()
    assert continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) is None


def test_overlap_economics_mismatch_fails_without_advancing_checkpoint(tmp_path) -> None:
    _, marketdata, _, continuity, _, _, value = service(
        tmp_path,
        payload([row(0, close="999"), row(1), row(2)]),
    )
    live = economics_bar(0)
    ticket = marketdata.record_finalized_for_strategy(
        live,
        strategy_id=STRATEGY,
        recorded_at=OBSERVED,
    )

    with pytest.raises(BybitRepairProtocolError, match="ECONOMICS_MISMATCH"):
        value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)

    assert marketdata.pending_decisions() == (ticket,)
    assert continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) is None


def test_repair_range_above_policy_fails_before_http(tmp_path) -> None:
    policy = BybitRepairPolicy(maximum_repair_bars=2)
    _, marketdata, _, continuity, transport, _, value = service(
        tmp_path,
        payload([row(0), row(1), row(2)]),
        policy=policy,
    )

    with pytest.raises(BybitRepairError, match="EXCEEDS_POLICY"):
        value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)

    assert transport.urls == []
    assert marketdata.pending_decisions() == ()
    assert continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) is None


def test_stale_and_future_server_clock_fail_before_persistence(tmp_path) -> None:
    for delta, reason in ((-91, "DATA_STALE"), (3, "CLOCK_IN_FUTURE")):
        case = tmp_path / reason
        case.mkdir()
        _, marketdata, _, continuity, _, _, value = service(
            case,
            payload(
                [row(0), row(1), row(2)],
                server_at=OBSERVED + timedelta(seconds=delta),
            ),
        )
        with pytest.raises(BybitRepairProtocolError, match=reason):
            value.repair(observed_at=OBSERVED, bootstrap_open_time=BASE)
        assert marketdata.pending_decisions() == ()
        assert continuity.latest(
            provider="BYBIT",
            venue="BYBIT_LINEAR",
            symbol="BTCUSDT",
            interval_seconds=300,
        ) is None


def test_transport_rejects_non_allowlisted_url_before_network() -> None:
    transport = StdlibBybitPublicHttpTransport()
    with pytest.raises(BybitRepairProtocolError, match="non-allowlisted"):
        transport.get(
            "https://api-testnet.bybit.com/v5/market/kline?symbol=BTCUSDT",
            timeout_seconds=1,
            maximum_response_bytes=4096,
        )


def test_checkpoint_retry_ignores_new_observation_time_but_remains_append_only(tmp_path) -> None:
    path, _, repair, continuity = stores(tmp_path)
    durable_bar = economics_bar(0)
    assert repair.record_without_decision(durable_bar, recorded_at=OBSERVED)
    checkpoint_id = continuity_checkpoint_id(
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=durable_bar.bar_id,
        through_close_time=durable_bar.close_time,
        evidence_source="TEST",
    )
    first = OperationalContinuityCheckpoint(
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=durable_bar.bar_id,
        through_close_time=durable_bar.close_time,
        established_at=OBSERVED,
        evidence_source="TEST",
    )
    repeated = OperationalContinuityCheckpoint(
        **{
            **first.__dict__,
            "established_at": OBSERVED + timedelta(seconds=30),
        }
    )
    assert continuity.append(first)
    assert not continuity.append(repeated)
    assert continuity.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) == first

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE operational_market_continuity SET evidence_source='TAMPERED'"
            )
    finally:
        connection.close()


def test_checkpoint_cannot_reference_missing_durable_bar(tmp_path) -> None:
    _, _, _, continuity = stores(tmp_path)
    through = BASE + timedelta(minutes=5)
    checkpoint_id = continuity_checkpoint_id(
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id="missing-bar",
        through_close_time=through,
        evidence_source="TEST",
    )
    invalid = OperationalContinuityCheckpoint(
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id="missing-bar",
        through_close_time=through,
        established_at=OBSERVED,
        evidence_source="TEST",
    )
    with pytest.raises(ValueError, match="through bar is missing"):
        continuity.append(invalid)
