from __future__ import annotations

import json
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
from app.oms.bybit_entry import (
    BybitEntrySubmissionClaim,
    bybit_entry_intent_id,
    bybit_reduce_only_intent_id,
)
from app.oms.store import OrderRecord, OrderState

NOW_MS = 1_800_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-1234567890ABCDEF"
CLOSE_LINK = "ASTRA-DEMO-H-1234567890ABCDEF"


class _MemoryEntryOms:
    live_mainnet_order_routing_allowed = False
    automatic_resubmit_after_submit_started_allowed = False

    def __init__(
        self,
        *,
        existing_state: OrderState | None = None,
        reduce_only: bool = False,
    ) -> None:
        self.record: OrderRecord | None = None
        self.uncertain_reasons: list[str] = []
        self.rejected_reasons: list[str] = []
        if existing_state is not None:
            self.record = _record(existing_state, reduce_only=reduce_only)

    def claim_entry_submission(self, intent, *, client_order_id, occurred_at):
        assert isinstance(intent, OrderIntent)
        assert client_order_id == ENTRY_LINK
        assert occurred_at == NOW
        return self._claim(intent, client_order_id=client_order_id)

    def claim_reduce_only_submission(self, intent, *, client_order_id, occurred_at):
        assert isinstance(intent, OrderIntent)
        assert client_order_id == CLOSE_LINK
        assert occurred_at == NOW
        return self._claim(intent, client_order_id=client_order_id)

    def _claim(self, intent: OrderIntent, *, client_order_id: str) -> BybitEntrySubmissionClaim:
        if self.record is None:
            self.record = OrderRecord(
                intent_id=intent.intent_id,
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

    def mark_rejected(
        self,
        intent_id,
        *,
        occurred_at,
        reason,
        broker_order_id=None,
    ):
        assert self.record is not None and intent_id == self.record.intent_id
        assert occurred_at == NOW
        self.rejected_reasons.append(reason)
        self.record = replace(
            self.record,
            state=OrderState.REJECTED,
            broker_order_id=self.record.broker_order_id if broker_order_id is None else broker_order_id,
        )
        return self.record

    def mark_uncertain(self, intent_id, *, occurred_at, reason):
        assert self.record is not None and intent_id == self.record.intent_id
        assert occurred_at == NOW
        self.uncertain_reasons.append(reason)
        self.record = replace(self.record, state=OrderState.UNCERTAIN)
        return self.record


def _record(state: OrderState, *, reduce_only: bool = False) -> OrderRecord:
    client_order_id = CLOSE_LINK if reduce_only else ENTRY_LINK
    intent_id = (
        bybit_reduce_only_intent_id(client_order_id)
        if reduce_only
        else bybit_entry_intent_id(client_order_id)
    )
    return OrderRecord(
        intent_id=intent_id,
        client_order_id=client_order_id,
        broker_order_id="" if state is OrderState.SUBMIT_STARTED else "broker-1",
        symbol="BTCUSDT",
        side=Side.SELL if reduce_only else Side.BUY,
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
        order_link_id=ENTRY_LINK,
    )


def _reduce_only_request(*, reference_price: Decimal | None = Decimal("60000")) -> BybitDemoOrderRequest:
    return BybitDemoOrderRequest(
        symbol="BTCUSDT",
        side="Sell",
        quantity=Decimal("0.01"),
        order_link_id=CLOSE_LINK,
        reduce_only=True,
        reference_price=reference_price,
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


def _ack_response(*, order_link_id: str = ENTRY_LINK) -> BybitDemoHttpJson:
    return BybitDemoHttpJson(
        200,
        {},
        {
            "retCode": 0,
            "result": {
                "orderId": "broker-1",
                "orderLinkId": order_link_id,
            },
        },
    )


def _recovery_response(*, rows: list[dict[str, Any]]) -> BybitDemoHttpJson:
    return BybitDemoHttpJson(200, {}, {"retCode": 0, "result": {"list": rows}})


def _broker_row(
    *,
    status: str = "Filled",
    cumulative_executed_quantity: str = "0.01",
    order_link_id: str = ENTRY_LINK,
    side: str = "Buy",
) -> dict[str, Any]:
    return {
        "orderId": "broker-1",
        "orderLinkId": order_link_id,
        "symbol": "BTCUSDT",
        "side": side,
        "qty": "0.01",
        "cumExecQty": cumulative_executed_quantity,
        "orderStatus": status,
        "rejectReason": "EC_NoError" if status != "Rejected" else "EC_NoEnoughQtyToFill",
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


def test_reduce_only_close_is_durably_claimed_before_exactly_one_post() -> None:
    oms = _MemoryEntryOms()
    calls: list[str] = []
    posted_payloads: list[dict[str, Any]] = []

    def transport(method, _url, _headers, body):
        calls.append(method)
        assert body is not None
        posted_payloads.append(json.loads(body))
        return _ack_response(order_link_id=CLOSE_LINK)

    ack = _client(oms=oms, transport=transport).place_market_order(_reduce_only_request())

    assert ack.order_id == "broker-1"
    assert calls == ["POST"]
    assert posted_payloads[0]["reduceOnly"] is True
    assert "reference_price" not in posted_payloads[0]
    assert "referencePrice" not in posted_payloads[0]
    assert oms.record is not None
    assert oms.record.intent_id == bybit_reduce_only_intent_id(CLOSE_LINK)
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


def test_resumed_reduce_only_submit_started_uses_get_only_and_never_posts_again() -> None:
    oms = _MemoryEntryOms(existing_state=OrderState.SUBMIT_STARTED, reduce_only=True)
    calls: list[str] = []

    def transport(method, _url, _headers, _body):
        calls.append(method)
        assert method == "GET"
        return _recovery_response(
            rows=[_broker_row(order_link_id=CLOSE_LINK, side="Sell")]
        )

    ack = _client(oms=oms, transport=transport).place_market_order(_reduce_only_request())

    assert ack.order_id == "broker-1"
    assert calls == ["GET"]
    assert oms.record is not None
    assert oms.record.state is OrderState.ACKNOWLEDGED


def test_cancelled_ambiguous_entry_never_becomes_safe_ack() -> None:
    oms = _MemoryEntryOms()

    def transport(method, _url, _headers, _body):
        if method == "POST":
            raise OSError("lost ack")
        return _recovery_response(
            rows=[_broker_row(status="Cancelled", cumulative_executed_quantity="0")]
        )

    with pytest.raises(BybitEntrySubmissionUncertainError, match="lifecycle reconciliation"):
        _client(oms=oms, transport=transport).place_market_order(_request())

    assert oms.record is not None
    assert oms.record.state is OrderState.UNCERTAIN


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


def test_reduce_only_close_without_reference_blocks_before_oms_claim_or_post() -> None:
    oms = _MemoryEntryOms()
    calls: list[str] = []

    with pytest.raises(ValueError, match="requires reference_price evidence"):
        _client(oms=oms, transport=lambda method, *_args: calls.append(method)).place_market_order(
            _reduce_only_request(reference_price=None)
        )

    assert calls == []
    assert oms.record is None
