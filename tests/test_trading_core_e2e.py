from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.application.paper_pipeline import PaperTradingPipeline
from app.domain.trading import Bar, Fill, Side
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.momentum import LongOnlyMomentumStrategy


UTC = timezone.utc
NOW = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)


def bars(*prices: str):
    return [
        Bar("AAPL", NOW + timedelta(minutes=index), Decimal(price))
        for index, price in enumerate(prices)
    ]


def pipeline(*, opening_cash="10000", max_order="1000"):
    return PaperTradingPipeline(
        strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1")),
        ledger=PortfolioLedger(opening_cash=Decimal(opening_cash)),
        risk=PreTradeRiskEngine(
            RiskLimits(
                maximum_order_notional=Decimal(max_order),
                maximum_symbol_notional=Decimal("2000"),
                maximum_gross_notional=Decimal("5000"),
            )
        ),
    )


def test_market_data_to_strategy_to_risk_to_fill_to_portfolio_e2e() -> None:
    runtime = pipeline()
    target, intent, decision = runtime.plan(bars("100", "101", "105"))
    assert target.quantity == Decimal("1")
    assert intent is not None and intent.side is Side.BUY
    assert decision is not None and decision.approved

    runtime.ledger.apply_fill(
        Fill(
            fill_id="fill-1",
            order_intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=intent.limit_price,
            occurred_at=intent.created_at,
        )
    )
    assert runtime.ledger.position("AAPL").quantity == Decimal("1")
    assert runtime.ledger.cash == Decimal("9895")
    assert runtime.ledger.equity({"AAPL": Decimal("105")}) == Decimal("10000")

    _, second_intent, second_decision = runtime.plan(bars("101", "102", "106"))
    assert second_intent is None
    assert second_decision is None


def test_falling_signal_generates_sell_to_flatten_existing_position() -> None:
    runtime = pipeline()
    first_target, first_intent, first_decision = runtime.plan(bars("100", "101", "105"))
    assert first_target.quantity == Decimal("1") and first_decision and first_decision.approved
    runtime.ledger.apply_fill(
        Fill("fill-1", first_intent.intent_id, "AAPL", Side.BUY, Decimal("1"), Decimal("105"), first_intent.created_at)
    )
    target, intent, decision = runtime.plan(bars("105", "104", "100"))
    assert target.quantity == Decimal("0")
    assert intent is not None and intent.side is Side.SELL and intent.quantity == Decimal("1")
    assert decision is not None


def test_risk_rejects_order_before_execution() -> None:
    runtime = pipeline(max_order="50")
    _, intent, decision = runtime.plan(bars("100", "101", "105"))
    assert intent is not None
    assert decision is not None and not decision.approved
    assert "ORDER_NOTIONAL_LIMIT_EXCEEDED" in decision.reasons
    assert runtime.ledger.position("AAPL").quantity == 0


def test_kill_switch_blocks_valid_trade() -> None:
    runtime = pipeline()
    _, _, decision = runtime.plan(bars("100", "101", "105"), kill_switch_engaged=True)
    assert decision is not None and not decision.approved
    assert decision.reasons == ("KILL_SWITCH_ENGAGED",)


def test_duplicate_fill_is_idempotent() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    fill = Fill("fill-1", "intent-1", "AAPL", Side.BUY, Decimal("1"), Decimal("100"), NOW)
    ledger.apply_fill(fill)
    ledger.apply_fill(fill)
    assert ledger.cash == Decimal("900")
    assert ledger.position("AAPL").quantity == Decimal("1")


def test_long_only_ledger_rejects_oversell_and_insufficient_cash() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("50"))
    with pytest.raises(ValueError, match="INSUFFICIENT_CASH"):
        ledger.apply_fill(Fill("f1", "i1", "AAPL", Side.BUY, Decimal("1"), Decimal("100"), NOW))
    with pytest.raises(ValueError, match="LONG_ONLY_POSITION_EXCEEDED"):
        ledger.apply_fill(Fill("f2", "i2", "AAPL", Side.SELL, Decimal("1"), Decimal("10"), NOW))


def test_strategy_rejects_duplicate_timestamps() -> None:
    strategy = LongOnlyMomentumStrategy()
    with pytest.raises(ValueError, match="duplicate bar timestamps"):
        strategy.target([
            Bar("AAPL", NOW, Decimal("100")),
            Bar("AAPL", NOW, Decimal("101")),
            Bar("AAPL", NOW + timedelta(minutes=1), Decimal("102")),
        ])
