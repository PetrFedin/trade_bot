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
    AlpacaTradeUpdateStreamV100,
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


def prepare_order(oms: IndexedDurableOmsStore, *, client_order_id: str = "client-1") -> None:
    intent = OrderIntent(
        intent_id="intent-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="trade-update-e2e",
    )
    oms.create(intent, client_order_id=client_order_id, occurred_at=NOW)
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


def fill_frame(
    *,
    client_order_id: str = "client-1",
    execution_id: str = "exec-1",
    event: str = "partial_fill",
    status: str = "partially_filled",
    cumulative: str = "1",
    qty: str = "1",
    price: str = "100",
) -> str:
    return json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": event,
                "execution_id": execution_id,
                "qty": qty,
                "price": price,
                "timestamp": "2026-08-07T18:30:01Z",
                "order": {
                    "id": "broker-1",
                    "client_order_id": client_order_id,
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "2",
                    "limit_price": "101",
                    "status": status,
                    "filled_qty": cumulative,
                    "updated_at": "2026-08-07T18:30:01Z",
                },
            },
        }
    )


def processor(tmp_path):
    oms = IndexedDurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    prepare_order(oms)
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    return (
        oms,
        portfolio,
        PaperTradeUpdateProcessor(
            stream=listening_stream(),
            oms=oms,
            fill_accounting=accounting,
        ),
    )


def test_account_wide_fill_routes_through_durable_client_order_index(tmp_path) -> None:
    oms, portfolio, service = processor(tmp_path)
    raw = fill_frame()
    result = service.process(raw, received_at=NOW, expected_generation=1)
    assert result.intent_id == "intent-1"
    assert result.stream_update is not None
    assert result.fill_accounting is not None
    assert result.fill_accounting.portfolio_event_appended
    assert result.fill_accounting.oms_advanced
    assert oms.get("intent-1").state is OrderState.PARTIALLY_FILLED
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("1")
    assert ledger.cash == Decimal("900")

    duplicate = service.process(raw, received_at=NOW, expected_generation=1)
    assert duplicate.stream_update is None
    assert duplicate.fill_accounting is not None
    assert duplicate.fill_accounting.portfolio_event_appended is False
    assert duplicate.fill_accounting.oms_advanced is False


def test_final_fill_completes_oms_and_portfolio(tmp_path) -> None:
    oms, portfolio, service = processor(tmp_path)
    service.process(fill_frame(), received_at=NOW, expected_generation=1)
    final = service.process(
        fill_frame(
            execution_id="exec-2",
            event="fill",
            status="filled",
            cumulative="2",
            price="102",
        ),
        received_at=NOW,
        expected_generation=1,
    )
    assert final.fill_accounting is not None
    assert final.fill_accounting.record.state is OrderState.FILLED
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("2")
    assert ledger.position("AAPL").average_cost == Decimal("101")
    assert ledger.cash == Decimal("798")


def test_unmapped_broker_fill_fails_closed(tmp_path) -> None:
    oms = IndexedDurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    service = PaperTradeUpdateProcessor(
        stream=listening_stream(),
        oms=oms,
        fill_accounting=PaperTradeFillAccounting(
            oms=oms,
            portfolio=portfolio,
            fee_provider=ExplicitZeroPaperFeeModel(),
        ),
    )
    with pytest.raises(UnmappedBrokerOrderError, match="missing-client"):
        service.process(
            fill_frame(client_order_id="missing-client"),
            received_at=NOW,
            expected_generation=1,
        )
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()


def test_non_fill_trade_update_does_not_touch_portfolio(tmp_path) -> None:
    _, portfolio, service = processor(tmp_path)
    raw = json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": "new",
                "order": {
                    "id": "broker-1",
                    "client_order_id": "client-1",
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "2",
                    "limit_price": "101",
                    "status": "new",
                    "filled_qty": "0",
                    "updated_at": "2026-08-07T18:30:00Z",
                },
            },
        }
    )
    result = service.process(raw, received_at=NOW, expected_generation=1)
    assert result.stream_update is not None
    assert result.fill_accounting is None
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()
