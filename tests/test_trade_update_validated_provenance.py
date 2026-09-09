from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.trade_updates import PaperTradeUpdateProcessor, UnmappedBrokerOrderError
from app.domain.trading import OrderIntent, Side
from app.execution.trade_fills import ExplicitZeroPaperFeeModel, PaperTradeFillAccounting
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.store import OrderState
from app.portfolio.store import PortfolioEventStore
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaPaperProtocolError,
    AlpacaTradeUpdateStreamV100,
    TradeStreamStateV100,
)

NOW = datetime(2026, 8, 7, 18, 30, tzinfo=UTC)


def credentials() -> AlpacaPaperCredentialsV100:
    return AlpacaPaperCredentialsV100(key_id="paper-key", secret_key="paper-secret")


def listening_stream() -> AlpacaTradeUpdateStreamV100:
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials())
    stream.authentication_frame()
    stream.ingest(
        json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
        received_at=NOW,
        expected_generation=1,
    )
    stream.ingest(
        json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
        received_at=NOW,
        expected_generation=1,
    )
    return stream


def prepare_order(oms: IndexedDurableOmsStore) -> None:
    intent = OrderIntent(
        intent_id="intent-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="validated-provenance",
    )
    oms.create(intent, client_order_id="client-1", occurred_at=NOW)
    oms.approve_risk(intent.intent_id, event_id="risk", occurred_at=NOW)
    oms.enqueue_submit(intent.intent_id, event_id="outbox", occurred_at=NOW)
    oms.transition(
        intent.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-start",
        occurred_at=NOW,
    )
    oms.transition(
        intent.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="ack",
        occurred_at=NOW,
        broker_order_id="broker-1",
    )


def fill_frame() -> str:
    return json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": "partial_fill",
                "execution_id": "exec-1",
                "qty": "1",
                "price": "100",
                "timestamp": "2026-08-07T18:30:01Z",
                "order": {
                    "id": "broker-1",
                    "client_order_id": "client-1",
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "2",
                    "limit_price": "101",
                    "status": "partially_filled",
                    "filled_qty": "1",
                    "updated_at": "2026-08-07T18:30:01Z",
                },
            },
        }
    )


def processor(tmp_path, stream: AlpacaTradeUpdateStreamV100):
    oms = IndexedDurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    return oms, portfolio, PaperTradeUpdateProcessor(
        stream=stream,
        oms=oms,
        fill_accounting=accounting,
    )


def test_unauthenticated_rejected_fill_duplicate_cannot_enter_accounting(tmp_path) -> None:
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials())
    stream.authentication_frame()
    oms, portfolio, service = processor(tmp_path, stream)
    prepare_order(oms)
    raw = fill_frame()

    with pytest.raises(AlpacaPaperProtocolError, match="update before listening"):
        service.process(raw, received_at=NOW, expected_generation=1)
    assert stream.state is TradeStreamStateV100.QUARANTINED
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()

    with pytest.raises(ValueError, match="TRADE_UPDATE_FILL_WITHOUT_VALIDATED_STREAM_PROVENANCE"):
        service.process(raw, received_at=NOW, expected_generation=1)
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()


def test_validated_duplicate_fill_remains_idempotently_replayable(tmp_path) -> None:
    oms, portfolio, service = processor(tmp_path, listening_stream())
    prepare_order(oms)
    raw = fill_frame()

    first = service.process(raw, received_at=NOW, expected_generation=1)
    duplicate = service.process(raw, received_at=NOW, expected_generation=1)

    assert first.stream_update is not None
    assert first.fill_accounting is not None
    assert first.fill_accounting.portfolio_event_appended
    assert duplicate.stream_update is None
    assert duplicate.fill_accounting is not None
    assert duplicate.fill_accounting.portfolio_event_appended is False
    assert portfolio.replay(opening_cash=Decimal("1000")).position("AAPL").quantity == Decimal("1")


def test_validated_fill_can_replay_after_downstream_mapping_failure(tmp_path) -> None:
    oms, portfolio, service = processor(tmp_path, listening_stream())
    raw = fill_frame()

    with pytest.raises(UnmappedBrokerOrderError, match="client-1"):
        service.process(raw, received_at=NOW, expected_generation=1)

    prepare_order(oms)
    replay = service.process(raw, received_at=NOW, expected_generation=1)

    assert replay.stream_update is None
    assert replay.fill_accounting is not None
    assert replay.fill_accounting.portfolio_event_appended
    assert portfolio.replay(opening_cash=Decimal("1000")).position("AAPL").quantity == Decimal("1")
