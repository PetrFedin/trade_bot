from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.trading import OrderIntent, Side
from app.execution.execution_facts import ExecutionFact, SQLiteExecutionFactStore
from app.execution.trade_fills import (
    ExactBrokerFill,
    ExplicitZeroPaperFeeModel,
    PaperTradeFillAccounting,
    TradeFillProtocolError,
    parse_alpaca_trade_fill,
)
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.store import PortfolioEventStore

NOW = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)


def intent(*, quantity: str = "2") -> OrderIntent:
    return OrderIntent(
        intent_id="intent-fill-accounting",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(quantity),
        limit_price=Decimal("101"),
        created_at=NOW,
        strategy_id="fill-e2e",
    )


def prepare_acknowledged(store: DurableOmsStore, *, quantity: str = "2") -> None:
    order = intent(quantity=quantity)
    store.create(order, client_order_id="client-fill", occurred_at=NOW)
    store.approve_risk(order.intent_id, event_id="risk", occurred_at=NOW)
    store.enqueue_submit(order.intent_id, event_id="outbox", occurred_at=NOW)
    store.transition(
        order.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-start",
        occurred_at=NOW,
    )
    store.transition(
        order.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="ack",
        occurred_at=NOW,
        broker_order_id="broker-fill",
    )


def frame(
    *,
    execution_id: str,
    event: str,
    qty: str,
    price: str,
    cumulative: str,
    symbol: str = "AAPL",
    client_order_id: str = "client-fill",
    broker_order_id: str = "broker-fill",
    order_qty: str = "2",
) -> str:
    return json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": event,
                "execution_id": execution_id,
                "qty": qty,
                "price": price,
                "timestamp": "2026-08-07T18:00:01Z",
                "order": {
                    "id": broker_order_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": "buy",
                    "qty": order_qty,
                    "filled_qty": cumulative,
                },
            },
        }
    )


def accounting(tmp_path: Path):
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    prepare_acknowledged(oms)
    service = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        execution_facts=SQLiteExecutionFactStore(tmp_path / "execution.sqlite"),
        opening_cash=Decimal("1000"),
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    return oms, portfolio, service


def test_exact_partial_and_final_fills_update_oms_and_portfolio(tmp_path: Path) -> None:
    oms, portfolio, service = accounting(tmp_path)

    first = parse_alpaca_trade_fill(
        frame(
            execution_id="exec-1",
            event="partial_fill",
            qty="1",
            price="100",
            cumulative="1",
        )
    )
    assert first is not None
    first_result = service.apply("intent-fill-accounting", first)
    assert first_result.portfolio_event_appended is True
    assert first_result.oms_advanced is True
    assert first_result.record.state is OrderState.PARTIALLY_FILLED
    assert first_result.record.filled_quantity == Decimal("1")

    duplicate = service.apply("intent-fill-accounting", first)
    assert duplicate.portfolio_event_appended is False
    assert duplicate.oms_advanced is False

    second = parse_alpaca_trade_fill(
        frame(
            execution_id="exec-2",
            event="fill",
            qty="1",
            price="102",
            cumulative="2",
        )
    )
    assert second is not None
    second_result = service.apply("intent-fill-accounting", second)
    assert second_result.record.state is OrderState.FILLED
    assert second_result.record.filled_quantity == Decimal("2")

    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("2")
    assert ledger.position("AAPL").average_cost == Decimal("101")
    assert ledger.cash == Decimal("798")
    assert oms.get("intent-fill-accounting").filled_quantity == Decimal("2")


def test_late_earlier_execution_repairs_portfolio_without_regressing_oms(tmp_path: Path) -> None:
    oms, portfolio, service = accounting(tmp_path)
    final = parse_alpaca_trade_fill(
        frame(
            execution_id="exec-final",
            event="fill",
            qty="1",
            price="102",
            cumulative="2",
        )
    )
    assert final is not None
    service.apply("intent-fill-accounting", final)
    assert oms.get("intent-fill-accounting").state is OrderState.FILLED

    earlier = parse_alpaca_trade_fill(
        frame(
            execution_id="exec-earlier",
            event="partial_fill",
            qty="1",
            price="100",
            cumulative="1",
        )
    )
    assert earlier is not None
    late = service.apply("intent-fill-accounting", earlier)
    assert late.portfolio_event_appended is True
    assert late.oms_advanced is False
    assert late.record.state is OrderState.FILLED
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("2")
    assert ledger.position("AAPL").average_cost == Decimal("101")


def test_fill_can_acknowledge_submit_started_before_advancing_quantity(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    order = intent(quantity="1")
    oms.create(order, client_order_id="client-fill", occurred_at=NOW)
    oms.approve_risk(order.intent_id, event_id="risk", occurred_at=NOW)
    oms.enqueue_submit(order.intent_id, event_id="outbox", occurred_at=NOW)
    oms.transition(
        order.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-start",
        occurred_at=NOW,
    )
    service = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        execution_facts=SQLiteExecutionFactStore(tmp_path / "execution.sqlite"),
        opening_cash=Decimal("1000"),
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    exact = parse_alpaca_trade_fill(
        frame(
            execution_id="exec-race",
            event="fill",
            qty="1",
            price="100",
            cumulative="1",
            order_qty="1",
        )
    )
    assert exact is not None
    result = service.apply(order.intent_id, exact)
    assert result.record.state is OrderState.FILLED
    assert result.record.broker_order_id == "broker-fill"


def test_parser_ignores_non_fill_events_and_rejects_missing_economics() -> None:
    non_fill = json.dumps({"stream": "trade_updates", "data": {"event": "new"}})
    assert parse_alpaca_trade_fill(non_fill) is None

    missing_execution = json.loads(
        frame(
            execution_id="exec-x",
            event="fill",
            qty="1",
            price="100",
            cumulative="1",
        )
    )
    del missing_execution["data"]["execution_id"]
    with pytest.raises(TradeFillProtocolError, match="execution_id is required"):
        parse_alpaca_trade_fill(json.dumps(missing_execution))

    missing_price = json.loads(
        frame(
            execution_id="exec-x",
            event="fill",
            qty="1",
            price="100",
            cumulative="1",
        )
    )
    del missing_price["data"]["price"]
    with pytest.raises(TradeFillProtocolError, match="missing decimal field: data.price"):
        parse_alpaca_trade_fill(json.dumps(missing_price))


def test_identity_mismatch_fails_before_portfolio_mutation(tmp_path: Path) -> None:
    _, portfolio, service = accounting(tmp_path)
    wrong_symbol = parse_alpaca_trade_fill(
        frame(
            execution_id="exec-wrong",
            event="partial_fill",
            qty="1",
            price="100",
            cumulative="1",
            symbol="MSFT",
        )
    )
    assert wrong_symbol is not None
    with pytest.raises(ValueError, match="BROKER_SYMBOL_MISMATCH"):
        service.apply("intent-fill-accounting", wrong_symbol)
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.positions() == ()


def test_submit_snapshot_coverage_prevents_late_exact_fill_double_count(tmp_path: Path) -> None:
    oms, portfolio, service = accounting(tmp_path)
    aggregate = ExactBrokerFill(
        execution_id="submit-snapshot",
        broker_order_id="broker-fill",
        client_order_id="client-fill",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("2"),
        cumulative_quantity=Decimal("2"),
        quantity=Decimal("2"),
        price=Decimal("101"),
        occurred_at=NOW + timedelta(seconds=1),
    )
    first = service.apply("intent-fill-accounting", aggregate)
    assert first.portfolio_event_appended
    assert first.record.state is OrderState.FILLED

    exact_first = ExactBrokerFill(
        execution_id="stream-exec-1",
        broker_order_id="broker-fill",
        client_order_id="client-fill",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("2"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW + timedelta(seconds=2),
    )
    exact_second = ExactBrokerFill(
        execution_id="stream-exec-2",
        broker_order_id="broker-fill",
        client_order_id="client-fill",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("2"),
        cumulative_quantity=Decimal("2"),
        quantity=Decimal("1"),
        price=Decimal("102"),
        occurred_at=NOW + timedelta(seconds=3),
    )
    late_first = service.apply("intent-fill-accounting", exact_first)
    late_second = service.apply("intent-fill-accounting", exact_second)
    assert not late_first.portfolio_event_appended
    assert not late_second.portfolio_event_appended
    assert not late_first.oms_advanced
    assert not late_second.oms_advanced

    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("2")
    assert ledger.position("AAPL").average_cost == Decimal("101")
    assert ledger.cash == Decimal("798")


def test_partial_execution_interval_overlap_fails_closed(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    facts = SQLiteExecutionFactStore(tmp_path / "execution.sqlite")
    prepare_acknowledged(oms)
    service = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        execution_facts=facts,
        opening_cash=Decimal("1000"),
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    aggregate = ExactBrokerFill(
        execution_id="aggregate-1",
        broker_order_id="broker-fill",
        client_order_id="client-fill",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("2"),
        cumulative_quantity=Decimal("1.5"),
        quantity=Decimal("1.5"),
        price=Decimal("100"),
        occurred_at=NOW + timedelta(seconds=1),
    )
    service.apply("intent-fill-accounting", aggregate)

    overlapping = ExactBrokerFill(
        execution_id="exact-overlap",
        broker_order_id="broker-fill",
        client_order_id="client-fill",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("2"),
        cumulative_quantity=Decimal("2"),
        quantity=Decimal("1"),
        price=Decimal("101"),
        occurred_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(ValueError, match="EXECUTION_CUMULATIVE_OVERLAP_CONFLICT"):
        service.apply("intent-fill-accounting", overlapping)

    assert oms.get("intent-fill-accounting").filled_quantity == Decimal("1.5")
    ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert ledger.position("AAPL").quantity == Decimal("1.5")
    assert ledger.cash == Decimal("850.0")
    assert facts.unresolved_count() == 1


def test_restart_after_portfolio_append_does_not_double_runtime_ledger(tmp_path: Path) -> None:
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    portfolio = PortfolioEventStore(tmp_path / "portfolio.sqlite")
    facts = SQLiteExecutionFactStore(tmp_path / "execution.sqlite")
    prepare_acknowledged(oms)
    fact = ExecutionFact(
        execution_fact_id="crash-window-fact",
        intent_id="intent-fill-accounting",
        broker_order_id="broker-fill",
        client_order_id="client-fill",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("2"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert facts.append(fact, source_execution_id="crash-window-source")
    assert portfolio.append_fill(fact.to_fill())
    runtime_ledger = portfolio.replay(opening_cash=Decimal("1000"))
    assert runtime_ledger.position("AAPL").quantity == Decimal("1")

    recovered = PaperTradeFillAccounting(
        oms=oms,
        portfolio=portfolio,
        execution_facts=facts,
        opening_cash=Decimal("1000"),
        fee_provider=ExplicitZeroPaperFeeModel(),
        runtime_ledger=runtime_ledger,
    ).recover_unresolved()

    assert len(recovered) == 1
    assert recovered[0].portfolio_event_appended is False
    assert recovered[0].oms_advanced is True
    assert facts.unresolved_count() == 0
    assert runtime_ledger.position("AAPL").quantity == Decimal("1")
    replayed = portfolio.replay(opening_cash=Decimal("1000"))
    assert replayed.position("AAPL").quantity == Decimal("1")
    assert replayed.cash == Decimal("900")
