from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain.trading import OrderIntent, Side
from app.execution.bybit_demo import BybitDemoHttpJson, BybitDemoOrderRequest
from app.execution.bybit_oms_entry_client import (
    BybitEntrySubmissionUncertainError,
    OmsAwareBybitDemoStopRatchetClient,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.marketdata.bybit_entry_reference import BybitEntryReferenceStore
from app.observability.bybit_runtime_health import BybitRestHealthRecorder
from app.oms.bybit_entry import BybitEntrySubmissionClaim, bybit_entry_intent_id
from app.oms.store import OrderRecord, OrderState

NOW_MS = 1_800_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)


class _MemoryEntryOms:
    live_mainnet_order_routing_allowed = False
    automatic_resubmit_after_submit_started_allowed = False

    def __init__(self, *, existing_state: OrderState | None = None) -> None:
        self.record: OrderRecord | None = None
        self.uncertain_reasons: list[str] = []
        self.rejected_reasons: list[str] = []
        if existing_state is not None:
            self.record = _record(existing_state)

    def claim_entry_submission(self, intent, *, client_order_id, occurred_at):
        assert isinstance(intent, OrderIntent)
        assert client_order_id == "ASTRA-DEMO-E-1234567890ABCDEF"
        assert occurred_at == NOW
        if self.record is None:
            self.record = _record(OrderState.SUBMIT_STARTED)
            return BybitEntrySubmissionClaim(self.record, True, True)
        return BybitEntrySubmissionClaim(self.record, False, False)

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
            state=OrderState.ACKNOWLEDGED,
            broker_order_id=broker_order_id,
        )
        return self.record

    def mark_rejected(self, intent_id, *, occurred_at, reason):
        assert self.record is not None and intent_id == self.record.intent_id
        assert occurred_at == NOW
        self.rejected_reasons.append(reason)
        self.record = replace(self.record, state=OrderState.REJECTED)
        return self.record

    def mark_uncertain(self, intent_id, *, occurred_at, reason):
        assert self.record is not None and intent_id == self.record.intent_id
        assert occurred_at == NOW
        self.uncertain_reasons.append(reason)
        self.record = replace(self.record, state=OrderState.UNCERTAIN)
        return self.record


def _record(state: OrderState) -> OrderRecord:
    return OrderRecord(
        intent_id=bybit_entry_intent_id("ASTRA-DEMO-E-1234567890ABCDEF"),
        client_order_id="ASTRA-DEMO-E-1234567890ABCDEF",
        broker_order_id="" if state is OrderState.SUBMIT_STARTED else "broker-1",
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("60001"),
        filled_quantity=Decimal("0"),
        state=state,
        version=4,
        updated_at=NOW,
    )


def _request() -> BybitDemoOrderRequest:
    return BybitDemoOrderRequest(
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        order_link_id="ASTRA-DEMO-E-1234567890ABCDEF",
    )


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


def _client(*, oms: _MemoryEntryOms, transport) -> OmsAwareBybitDemoStopRatchetClient:
    return OmsAwareBybitDemoStopRatchetClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: NOW_MS,
        sleep_fn=lambda _delay: None,
        rest_health_sink=BybitRestHealthRecorder(),
        entry_oms=oms,
        entry_reference_store=_reference_store(),
    )


def _ack_response() -> BybitDemoHttpJson:
    return BybitDemoHttpJson(
        200,
        {},
        {
            "retCode": 0,
            "result": {
                "orderId": "broker-1",
                "orderLinkId": "ASTRA-DEMO-E-1234567890ABCDEF",
            },
        },
    )


def _recovery_response(*, rows: list[dict[str, Any]]) -> BybitDemoHttpJson:
    return BybitDemoHttpJson(
        200,
        {},
        {"retCode": 0, "result": {"list": rows}},
    )


def _broker_row() -> dict[str, Any]:
    return {
        "orderId": "broker-1",
        "orderLinkId": "ASTRA-DEMO-E-1234567890ABCDEF",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "qty": "0.01",
        "orderStatus": "Filled",
    }


def test_entry_is_durably_claimed_before_exactly_one_post() -> None:
    oms = _MemoryEntryOms()
    calls: list[str] = []

    def transport(method, _url, _headers, _body):
        calls.append(method)
        return _ack_response()

    ack = _client(oms=oms, transport=transport).place_market_order(_request())

    assert ack.order_id == "broker-1"
    assert calls == ["POST"]
    assert oms.record is not None
    assert oms.record.state is OrderState.ACKNOWLEDGED


def test_ambiguous_post_recovers_by_order_link_id_without_second_post() -> None:
    oms = _MemoryEntryOms()
    calls: list[str] = []

    def transport(method, url, _headers, _body):
        calls.append(method)
        if method == "POST":
            raise OSError("lost ack")
        assert "/v5/order/realtime" in url
        return _recovery_response(rows=[_broker_row()])

    ack = _client(oms=oms, transport=transport).place_market_order(_request())

    assert ack.order_id == "broker-1"
    assert calls.count("POST") == 1
    assert calls.count("GET") == 1
    assert oms.record is not None
    assert oms.record.state is OrderState.ACKNOWLEDGED


def test_ambiguous_post_without_broker_truth_becomes_durable_uncertain() -> None:
    oms = _MemoryEntryOms()
    calls: list[str] = []

    def transport(method, _url, _headers, _body):
        calls.append(method)
        if method == "POST":
            raise OSError("lost ack")
        return _recovery_response(rows=[])

    with pytest.raises(BybitEntrySubmissionUncertainError, match="GET-only recovery"):
        _client(oms=oms, transport=transport).place_market_order(_request())

    assert calls.count("POST") == 1
    assert calls.count("GET") == 2
    assert oms.record is not None
    assert oms.record.state is OrderState.UNCERTAIN
    assert oms.uncertain_reasons


def test_resumed_submit_started_uses_get_only_and_never_posts_again() -> None:
    oms = _MemoryEntryOms(existing_state=OrderState.SUBMIT_STARTED)
    calls: list[str] = []

    def transport(method, _url, _headers, _body):
        calls.append(method)
        assert method == "GET"
        return _recovery_response(rows=[_broker_row()])

    ack = _client(oms=oms, transport=transport).place_market_order(_request())

    assert ack.order_id == "broker-1"
    assert calls == ["GET"]
    assert oms.record is not None
    assert oms.record.state is OrderState.ACKNOWLEDGED


def test_missing_pre_entry_reference_blocks_before_oms_claim_or_post() -> None:
    oms = _MemoryEntryOms()
    calls: list[str] = []
    client = OmsAwareBybitDemoStopRatchetClient(
        api_key="key",
        api_secret="secret",
        transport=lambda method, *_args: calls.append(method),
        clock_ms=lambda: NOW_MS,
        rest_health_sink=BybitRestHealthRecorder(),
        entry_oms=oms,
        entry_reference_store=BybitEntryReferenceStore(),
    )

    with pytest.raises(RuntimeError, match="REFERENCE_UNAVAILABLE"):
        client.place_market_order(_request())

    assert calls == []
    assert oms.record is None
