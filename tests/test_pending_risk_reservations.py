from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.composition import ProductConfig, build_local_product
from app.application.paper_cycle import PaperCycleService
from app.domain.trading import Bar, OrderIntent, Side
from app.execution.trade_fills import ExplicitZeroPaperFeeModel
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.risk_reservations import RiskReservationBudget, RiskReservationRejected
from app.oms.store import OrderState
from app.risk.pretrade import RiskLimits
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def tight_config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("110"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("110"),
            maximum_symbol_notional=Decimal("110"),
            maximum_gross_notional=Decimal("110"),
        ),
    )


def bars(offset_minutes: int) -> list[Bar]:
    end = NOW + timedelta(minutes=offset_minutes)
    return [
        Bar("AAPL", end - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", end - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", end, Decimal("102")),
    ]


def listening_stream() -> AlpacaTradeUpdateStreamV100:
    credentials = AlpacaPaperCredentialsV100(
        key_id="paper-key",
        secret_key="paper-secret",
    )
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials)
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


class NoSubmitBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0

    def submit_limit_order(self, **kwargs):
        self.submit_calls += 1
        raise AssertionError("reservation qualification must not submit")

    def get_order_by_client_order_id(self, client_order_id: str):
        return None


def reservation_budget() -> RiskReservationBudget:
    return RiskReservationBudget(
        available_cash=Decimal("110"),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        maximum_symbol_notional=Decimal("110"),
        maximum_gross_notional=Decimal("110"),
    )


def reservation_intent(intent_id: str) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("102"),
        created_at=NOW,
        strategy_id="reservation-race",
    )


def test_product_reserves_first_pending_buy_and_rejects_second_and_third(tmp_path) -> None:
    runtime = build_local_product(
        config=tight_config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    broker = NoSubmitBroker()
    cycle = PaperCycleService(
        runtime=runtime,
        broker=broker,
        trade_stream=listening_stream(),
        stream_generation=1,
    )

    first = cycle.plan_and_prepare(bars(0))
    second = cycle.plan_and_prepare(bars(10))
    third = cycle.plan_and_prepare(bars(20))

    assert first.order_ready
    assert first.prepared is not None
    assert first.prepared.record.state is OrderState.OUTBOXED
    assert second.intent is not None and second.prepared is None
    assert third.intent is not None and third.prepared is None
    expected = (
        "GROSS_NOTIONAL_LIMIT_EXCEEDED",
        "INSUFFICIENT_AVAILABLE_CASH",
        "SYMBOL_NOTIONAL_LIMIT_EXCEEDED",
    )
    assert second.risk is not None and second.risk.reasons == expected
    assert third.risk is not None and third.risk.reasons == expected
    assert len(runtime.oms_store.pending_outbox()) == 1
    assert broker.submit_calls == 0


def test_sqlite_concurrent_approvals_share_one_pending_capacity_reservation(tmp_path) -> None:
    path = tmp_path / "reservation-race.sqlite"
    seed = IndexedDurableOmsStore(path)
    for suffix in ("a", "b"):
        value = reservation_intent(f"reserve-{suffix}")
        seed.create(
            value,
            client_order_id=f"client-{suffix}",
            occurred_at=NOW,
        )

    def reserve(intent_id: str) -> tuple[str, str]:
        store = IndexedDurableOmsStore(path)
        try:
            record = store.approve_risk_with_reservation(
                intent_id,
                event_id=f"risk:{intent_id}",
                occurred_at=NOW,
                budget=reservation_budget(),
            )
            return intent_id, record.state.value
        except RiskReservationRejected as exc:
            return intent_id, f"REJECTED:{','.join(exc.reasons)}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = dict(executor.map(reserve, ("reserve-a", "reserve-b")))

    assert list(results.values()).count(OrderState.RISK_APPROVED.value) == 1
    assert sum(value.startswith("REJECTED:") for value in results.values()) == 1
    states = {
        intent_id: seed.get(intent_id).state  # type: ignore[union-attr]
        for intent_id in ("reserve-a", "reserve-b")
    }
    assert list(states.values()).count(OrderState.RISK_APPROVED) == 1
    assert list(states.values()).count(OrderState.CREATED) == 1


def test_partially_filled_buy_reserves_only_remaining_notional(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "partial.sqlite")
    first = OrderIntent(
        intent_id="partial-first",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("50"),
        created_at=NOW,
        strategy_id="reservation-partial",
    )
    store.create(first, client_order_id="partial-first-client", occurred_at=NOW)
    store.approve_risk_with_reservation(
        first.intent_id,
        event_id="risk:partial-first",
        occurred_at=NOW,
        budget=RiskReservationBudget(
            available_cash=Decimal("125"),
            current_symbol_notional=Decimal("0"),
            current_gross_notional=Decimal("0"),
            maximum_symbol_notional=Decimal("125"),
            maximum_gross_notional=Decimal("125"),
        ),
    )
    store.enqueue_submit(first.intent_id, event_id="outbox:partial-first", occurred_at=NOW)
    store.transition(
        first.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit:partial-first",
        occurred_at=NOW,
    )
    store.transition(
        first.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="ack:partial-first",
        occurred_at=NOW,
        broker_order_id="partial-broker",
    )
    store.apply_cumulative_fill(
        first.intent_id,
        event_id="fill:partial-first",
        cumulative_filled=Decimal("1"),
        occurred_at=NOW,
    )

    second = OrderIntent(
        intent_id="partial-second",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("50"),
        created_at=NOW,
        strategy_id="reservation-partial",
    )
    store.create(second, client_order_id="partial-second-client", occurred_at=NOW)
    approved = store.approve_risk_with_reservation(
        second.intent_id,
        event_id="risk:partial-second",
        occurred_at=NOW,
        budget=RiskReservationBudget(
            available_cash=Decimal("100"),
            current_symbol_notional=Decimal("50"),
            current_gross_notional=Decimal("50"),
            maximum_symbol_notional=Decimal("150"),
            maximum_gross_notional=Decimal("150"),
        ),
    )
    assert approved.state is OrderState.RISK_APPROVED
