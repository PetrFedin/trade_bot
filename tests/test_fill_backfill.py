from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from app.domain.trading import OrderIntent, Side
from app.execution.alpaca_fill_backfill import (
    AlpacaFillActivity,
    AlpacaPaperFillActivityReader,
    FillActivityPage,
    FillActivityRecoveryError,
    FillBackfillPolicy,
    PaperFillBackfillService,
)
from app.execution.trade_fills import (
    ExactBrokerFill,
    ExplicitZeroPaperFeeModel,
    PaperTradeFillAccounting,
)
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.store import OrderState
from app.portfolio.strict import StrictPortfolioEventStore
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaPaperPolicyV100,
    AlpacaPaperProtocolError,
    HttpResponseV100,
)

NOW = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)


class QueueTransport:
    def __init__(self, responses: list[HttpResponseV100 | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class StaticActivitySource:
    def __init__(self, pages: list[FillActivityPage]) -> None:
        self.pages = list(pages)
        self.calls: list[str | None] = []

    def page(self, *, after, until, page_size, page_token):
        self.calls.append(page_token)
        return self.pages.pop(0)


def activity_payload(
    *,
    activity_id: str = "activity-1",
    order_id: str = "broker-1",
    symbol: str = "AAPL",
    side: str = "buy",
    cum_qty: str = "1",
    qty: str = "1",
    price: str = "100",
    transaction_time: str = "2026-08-07T18:00:01Z",
    kind: str = "fill",
) -> dict[str, str]:
    return {
        "activity_type": "FILL",
        "id": activity_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "cum_qty": cum_qty,
        "qty": qty,
        "price": price,
        "transaction_time": transaction_time,
        "type": kind,
    }


def activity(
    *,
    activity_id: str = "activity-1",
    order_id: str = "broker-1",
    symbol: str = "AAPL",
    cumulative: str = "1",
    quantity: str = "1",
    price: str = "100",
    occurred_at: datetime = NOW + timedelta(seconds=1),
) -> AlpacaFillActivity:
    return AlpacaFillActivity(
        activity_id=activity_id,
        broker_order_id=order_id,
        symbol=symbol,
        side=Side.BUY,
        cumulative_quantity=Decimal(cumulative),
        quantity=Decimal(quantity),
        price=Decimal(price),
        occurred_at=occurred_at,
        activity_kind="fill",
    )


def prepare_order(oms: IndexedDurableOmsStore) -> OrderIntent:
    intent = OrderIntent(
        intent_id="intent-backfill",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="backfill-e2e",
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
    return intent


def recovery_stack(tmp_path, source: StaticActivitySource):
    tmp_path.mkdir(parents=True, exist_ok=True)
    oms = IndexedDurableOmsStore(tmp_path / "oms.sqlite")
    prepare_order(oms)
    portfolio = StrictPortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    accounting = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=ledger,
    )
    service = PaperFillBackfillService(source=source, oms=oms, accounting=accounting)
    return oms, portfolio, ledger, accounting, service


def credentials() -> AlpacaPaperCredentialsV100:
    return AlpacaPaperCredentialsV100(key_id="key", secret_key="secret")


def test_reader_is_get_only_and_encodes_bounded_query() -> None:
    body = json.dumps([activity_payload()]).encode()
    transport = QueueTransport([HttpResponseV100(200, {}, body)])
    reader = AlpacaPaperFillActivityReader(credentials=credentials(), transport=transport)
    page = reader.page(
        after=NOW,
        until=NOW + timedelta(hours=1),
        page_size=1,
        page_token="previous::token",
    )
    assert len(page.activities) == 1
    assert page.next_page_token == "activity-1"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["body"] is None
    parsed = urlparse(str(call["url"]))
    assert parsed.path == "/v2/account/activities/FILL"
    query = parse_qs(parsed.query)
    assert query["direction"] == ["asc"]
    assert query["page_size"] == ["1"]
    assert query["page_token"] == ["previous::token"]
    assert query["after"] == ["2026-08-07T18:00:00Z"]


def test_reader_retries_read_only_transient_status_with_per_attempt_calls() -> None:
    transport = QueueTransport(
        [
            HttpResponseV100(503, {}, b"{}"),
            HttpResponseV100(200, {}, b"[]"),
        ]
    )
    sleeps: list[float] = []
    reader = AlpacaPaperFillActivityReader(
        credentials=credentials(),
        transport=transport,
        policy=AlpacaPaperPolicyV100(
            maximum_read_attempts=2,
            initial_backoff_seconds=0,
            read_capacity=2,
            read_refill_per_second=Decimal("1"),
        ),
        sleeper=sleeps.append,
    )
    page = reader.page(
        after=NOW,
        until=NOW + timedelta(minutes=1),
        page_size=100,
        page_token=None,
    )
    assert page.activities == ()
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]
    assert sleeps == [0]


def test_reader_rejects_oversized_or_invalid_payloads() -> None:
    oversized = QueueTransport([HttpResponseV100(200, {}, b"x" * 9)])
    reader = AlpacaPaperFillActivityReader(
        credentials=credentials(),
        transport=oversized,
        policy=AlpacaPaperPolicyV100(maximum_response_bytes=8),
    )
    with pytest.raises(AlpacaPaperProtocolError, match="exceeds configured size"):
        reader.page(
            after=NOW,
            until=NOW + timedelta(minutes=1),
            page_size=100,
            page_token=None,
        )

    invalid = QueueTransport([HttpResponseV100(200, {}, b"not-json")])
    reader = AlpacaPaperFillActivityReader(credentials=credentials(), transport=invalid)
    with pytest.raises(AlpacaPaperProtocolError, match="invalid fill activities JSON"):
        reader.page(
            after=NOW,
            until=NOW + timedelta(minutes=1),
            page_size=100,
            page_token=None,
        )


def test_backfill_recovers_missed_fill_into_oms_and_runtime_portfolio(tmp_path) -> None:
    source = StaticActivitySource([FillActivityPage((activity(),), None)])
    oms, _, ledger, _, service = recovery_stack(tmp_path, source)
    result = service.recover(after=NOW, until=NOW + timedelta(minutes=1))
    assert result.complete
    assert result.activities_seen == 1
    assert result.portfolio_events_appended == 1
    assert result.oms_advances == 1
    assert oms.get("intent-backfill").state is OrderState.FILLED
    assert ledger.position("AAPL").quantity == Decimal("1")
    assert ledger.position("AAPL").average_cost == Decimal("100")
    assert ledger.cash == Decimal("900")


def test_stream_then_activity_same_economics_is_not_double_counted(tmp_path) -> None:
    source = StaticActivitySource([FillActivityPage((activity(),), None)])
    oms, portfolio, ledger, accounting, service = recovery_stack(tmp_path, source)
    stream_fill = ExactBrokerFill(
        execution_id="websocket-execution-id",
        broker_order_id="broker-1",
        client_order_id="client-1",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW + timedelta(seconds=1),
    )
    first = accounting.apply("intent-backfill", stream_fill)
    assert first.portfolio_event_appended
    result = service.recover(after=NOW, until=NOW + timedelta(minutes=1))
    assert result.complete
    assert result.portfolio_events_appended == 0
    assert result.duplicate_portfolio_events == 1
    assert result.oms_advances == 0
    assert ledger.position("AAPL").quantity == Decimal("1")
    replayed = portfolio.replay(opening_cash=Decimal("1000"))
    assert replayed.position("AAPL").quantity == Decimal("1")
    assert replayed.cash == Decimal("900")
    assert oms.get("intent-backfill").state is OrderState.FILLED


def test_unmapped_activity_is_reported_without_portfolio_mutation(tmp_path) -> None:
    source = StaticActivitySource(
        [FillActivityPage((activity(order_id="manual-or-unknown-order"),), None)]
    )
    _, portfolio, ledger, _, service = recovery_stack(tmp_path, source)
    result = service.recover(after=NOW, until=NOW + timedelta(minutes=1))
    assert not result.complete
    assert result.reasons == ("UNRESOLVED_BROKER_ORDERS",)
    assert result.unresolved_broker_order_ids == ("manual-or-unknown-order",)
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()
    assert ledger.positions() == ()


def test_backfill_limits_fail_before_applying_collected_events(tmp_path) -> None:
    page = FillActivityPage((activity(),), "activity-1")
    source = StaticActivitySource([page])
    _, portfolio, ledger, _, service = recovery_stack(tmp_path, source)
    bounded = PaperFillBackfillService(
        source=source,
        oms=service.oms,
        accounting=service.accounting,
        policy=FillBackfillPolicy(maximum_pages=1, maximum_activities=10, page_size=1),
    )
    result = bounded.recover(after=NOW, until=NOW + timedelta(minutes=1))
    assert not result.complete
    assert result.reasons == ("PAGE_LIMIT_REACHED",)
    assert result.portfolio_events_appended == 0
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()
    assert ledger.positions() == ()


def test_duplicate_activity_ids_and_identity_mismatch_fail_closed(tmp_path) -> None:
    duplicate_source = StaticActivitySource(
        [
            FillActivityPage((activity(activity_id="same"),), "same"),
            FillActivityPage((activity(activity_id="same"),), None),
        ]
    )
    _, _, _, _, duplicate_service = recovery_stack(tmp_path / "dup", duplicate_source)
    with pytest.raises(FillActivityRecoveryError, match="duplicate fill activity"):
        duplicate_service.recover(after=NOW, until=NOW + timedelta(minutes=1))

    mismatch_source = StaticActivitySource(
        [FillActivityPage((activity(symbol="MSFT"),), None)]
    )
    _, portfolio, _, _, mismatch_service = recovery_stack(
        tmp_path / "mismatch",
        mismatch_source,
    )
    with pytest.raises(ValueError, match="BROKER_SYMBOL_MISMATCH"):
        mismatch_service.recover(after=NOW, until=NOW + timedelta(minutes=1))
    assert portfolio.replay(opening_cash=Decimal("1000")).positions() == ()


def test_backfill_window_and_timestamp_inputs_are_bounded(tmp_path) -> None:
    source = StaticActivitySource([FillActivityPage((), None)])
    _, _, _, _, service = recovery_stack(tmp_path, source)
    with pytest.raises(ValueError, match="maximum"):
        service.recover(after=NOW, until=NOW + timedelta(days=8))
    with pytest.raises(ValueError, match="timezone-aware"):
        service.recover(
            after=NOW.replace(tzinfo=None),
            until=NOW + timedelta(minutes=1),
        )
