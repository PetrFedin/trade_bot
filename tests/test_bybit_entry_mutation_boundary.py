from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.trading import OrderIntent, Side
from app.execution.bybit_demo import BybitDemoHttpJson, BybitDemoOrderRequest
from app.execution.bybit_oms_entry_client import (
    BybitEntryRecoveryEnvelopeError,
    OmsAwareBybitDemoStopRatchetClient,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.marketdata.bybit_entry_reference import BybitEntryReferenceStore
from app.observability.bybit_runtime_health import BybitRestHealthRecorder
from app.oms.bybit_entry import BybitEntrySubmissionClaim, bybit_entry_intent_id
from app.oms.store import OrderRecord, OrderState

NOW_MS = 1_800_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-MUTATION-BOUNDARY"
CLOSE_LINK = "ASTRA-DEMO-H-MUTATION-BOUNDARY"


class _Envelope:
    def __init__(
        self,
        *,
        order_link_id: str = ENTRY_LINK,
        symbol: str = "BTCUSDT",
        side: str = "Buy",
        quantity: Decimal = Decimal("0.01"),
        valid: bool = True,
    ) -> None:
        self.entry_order_link_id = order_link_id
        self.trade_plan = SimpleNamespace(symbol=symbol)
        self.order_side = side
        self.approved_order_quantity = quantity
        self.valid = valid

    def validate(self) -> None:
        if not self.valid:
            raise ValueError("invalid frozen envelope")


class _RecoveryStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(
        self,
        *,
        envelope: _Envelope | None = None,
        failure: Exception | None = None,
        record_sha256: str = "a" * 64,
        record_mainnet_allowed: bool = False,
    ) -> None:
        self.envelope = _Envelope() if envelope is None else envelope
        self.failure = failure
        self.record_sha256 = record_sha256
        self.record_mainnet_allowed = record_mainnet_allowed
        self.events: list[str] = []

    def load(self, *, entry_order_link_id: str):
        self.events.append("load")
        assert entry_order_link_id == ENTRY_LINK
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(
            envelope=self.envelope,
            record_sha256=self.record_sha256,
            live_mainnet_order_routing_allowed=self.record_mainnet_allowed,
        )


class _Oms:
    live_mainnet_order_routing_allowed = False
    automatic_resubmit_after_submit_started_allowed = False

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.record: OrderRecord | None = None

    def claim_entry_submission(self, intent, *, client_order_id, occurred_at):
        self.events.append("claim-entry")
        assert isinstance(intent, OrderIntent)
        assert client_order_id == ENTRY_LINK
        assert occurred_at == NOW
        self.record = OrderRecord(
            intent_id=bybit_entry_intent_id(client_order_id),
            client_order_id=client_order_id,
            broker_order_id="",
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            filled_quantity=Decimal("0"),
            state=OrderState.SUBMIT_STARTED,
            version=4,
            updated_at=NOW,
        )
        return BybitEntrySubmissionClaim(self.record, True, True)

    def claim_reduce_only_submission(self, intent, *, client_order_id, occurred_at):
        self.events.append("claim-close")
        assert isinstance(intent, OrderIntent)
        assert client_order_id == CLOSE_LINK
        assert occurred_at == NOW
        self.record = OrderRecord(
            intent_id=f"bybit-reduce-only:{client_order_id}",
            client_order_id=client_order_id,
            broker_order_id="",
            symbol=intent.symbol,
            side=Side.SELL,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            filled_quantity=Decimal("0"),
            state=OrderState.SUBMIT_STARTED,
            version=4,
            updated_at=NOW,
        )
        return BybitEntrySubmissionClaim(self.record, True, True)

    def mark_acknowledged(
        self,
        intent_id,
        *,
        broker_order_id,
        occurred_at,
        recovered_by_read,
    ):
        assert self.record is not None
        assert intent_id == self.record.intent_id
        assert occurred_at == NOW
        self.record = replace(
            self.record,
            broker_order_id=broker_order_id,
            state=OrderState.ACKNOWLEDGED,
        )
        return self.record

    def mark_rejected(self, *_args, **_kwargs):
        raise AssertionError("unexpected reject path")

    def mark_uncertain(self, *_args, **_kwargs):
        raise AssertionError("unexpected uncertain path")


def _reference_store() -> BybitEntryReferenceStore:
    store = BybitEntryReferenceStore()
    store.record(
        BybitDemoMarketQuote(
            symbol="BTCUSDT",
            last_price=Decimal("60000"),
            mark_price=Decimal("60000"),
            bid_price=Decimal("59999"),
            ask_price=Decimal("60001"),
            server_time_ms=NOW_MS - 100,
            received_time_ms=NOW_MS - 50,
            age_ms=50,
        )
    )
    return store


def _entry_request() -> BybitDemoOrderRequest:
    return BybitDemoOrderRequest(
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        order_link_id=ENTRY_LINK,
    )


def _close_request() -> BybitDemoOrderRequest:
    return BybitDemoOrderRequest(
        symbol="BTCUSDT",
        side="Sell",
        quantity=Decimal("0.01"),
        order_link_id=CLOSE_LINK,
        reduce_only=True,
        reference_price=Decimal("60000"),
    )


def _ack(order_link_id: str) -> BybitDemoHttpJson:
    return BybitDemoHttpJson(
        200,
        {},
        {"retCode": 0, "result": {"orderId": "broker-1", "orderLinkId": order_link_id}},
    )


def _client(*, store: _RecoveryStore, events: list[str]):
    oms = _Oms(events)

    def transport(method, _url, _headers, _body):
        events.append(method)
        return _ack(CLOSE_LINK if "claim-close" in events else ENTRY_LINK)

    return (
        OmsAwareBybitDemoStopRatchetClient(
            api_key="key",
            api_secret="secret",
            transport=transport,
            clock_ms=lambda: NOW_MS,
            sleep_fn=lambda _delay: None,
            rest_health_sink=BybitRestHealthRecorder(),
            entry_oms=oms,
            entry_reference_store=_reference_store(),
            entry_recovery_store=store,
        ),
        oms,
    )


def test_exact_immutable_envelope_is_verified_before_oms_claim_and_entry_post() -> None:
    events: list[str] = []
    store = _RecoveryStore()
    client, oms = _client(store=store, events=events)

    ack = client.place_market_order(_entry_request())

    assert ack.order_id == "broker-1"
    assert store.events == ["load"]
    assert events == ["claim-entry", "POST"]
    assert oms.record is not None
    assert oms.record.state is OrderState.ACKNOWLEDGED


def test_missing_recovery_envelope_blocks_before_oms_claim_or_entry_post() -> None:
    events: list[str] = []
    store = _RecoveryStore(failure=FileNotFoundError("missing"))
    client, oms = _client(store=store, events=events)

    with pytest.raises(BybitEntryRecoveryEnvelopeError, match="LOAD_FAILED:FileNotFoundError"):
        client.place_market_order(_entry_request())

    assert store.events == ["load"]
    assert events == []
    assert oms.record is None


@pytest.mark.parametrize(
    ("envelope", "reason"),
    [
        (_Envelope(order_link_id="ASTRA-DEMO-E-OTHER"), "ORDER_LINK_ID"),
        (_Envelope(symbol="ETHUSDT"), "SYMBOL"),
        (_Envelope(side="Sell"), "SIDE"),
        (_Envelope(quantity=Decimal("0.02")), "QUANTITY"),
    ],
)
def test_recovery_envelope_request_drift_blocks_before_oms_claim_or_entry_post(
    envelope: _Envelope,
    reason: str,
) -> None:
    events: list[str] = []
    client, oms = _client(store=_RecoveryStore(envelope=envelope), events=events)

    with pytest.raises(BybitEntryRecoveryEnvelopeError, match=reason):
        client.place_market_order(_entry_request())

    assert events == []
    assert oms.record is None


def test_invalid_recovery_envelope_blocks_before_oms_claim_or_entry_post() -> None:
    events: list[str] = []
    client, oms = _client(
        store=_RecoveryStore(envelope=_Envelope(valid=False)),
        events=events,
    )

    with pytest.raises(BybitEntryRecoveryEnvelopeError, match="VALIDATION_FAILED:ValueError"):
        client.place_market_order(_entry_request())

    assert events == []
    assert oms.record is None


@pytest.mark.parametrize("record_sha256", ["", "a" * 63, "A" * 64, "g" * 64])
def test_invalid_recovery_record_checksum_metadata_blocks_before_oms_claim_or_post(
    record_sha256: str,
) -> None:
    events: list[str] = []
    client, oms = _client(
        store=_RecoveryStore(record_sha256=record_sha256),
        events=events,
    )

    with pytest.raises(BybitEntryRecoveryEnvelopeError, match="RECORD_SHA256_INVALID"):
        client.place_market_order(_entry_request())

    assert events == []
    assert oms.record is None


def test_mainnet_capable_recovery_record_blocks_before_oms_claim_or_post() -> None:
    events: list[str] = []
    client, oms = _client(
        store=_RecoveryStore(record_mainnet_allowed=True),
        events=events,
    )

    with pytest.raises(BybitEntryRecoveryEnvelopeError, match="REJECTED_MAINNET_CAPABILITY"):
        client.place_market_order(_entry_request())

    assert events == []
    assert oms.record is None


def test_reduce_only_close_does_not_depend_on_entry_recovery_envelope() -> None:
    events: list[str] = []
    store = _RecoveryStore(failure=AssertionError("ENTRY envelope must not be read for close"))
    client, oms = _client(store=store, events=events)

    ack = client.place_market_order(_close_request())

    assert ack.order_id == "broker-1"
    assert store.events == []
    assert events == ["claim-close", "POST"]
    assert oms.record is not None
    assert oms.record.state is OrderState.ACKNOWLEDGED
