from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.trading import Bar, OrderIntent, Side
from app.marketdata.validation import MarketDataPolicy, validate_bar_series
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskLimits

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def order() -> OrderIntent:
    return OrderIntent(
        intent_id="risk-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("102"),
        created_at=NOW,
        strategy_id="validation",
    )


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_order_notional=Decimal("500"),
        maximum_symbol_notional=Decimal("1000"),
        maximum_gross_notional=Decimal("2000"),
        maximum_price_age_seconds=Decimal("10"),
        maximum_spread_bps=Decimal("20"),
        maximum_slippage_bps=Decimal("25"),
        maximum_daily_loss=Decimal("100"),
        maximum_drawdown=Decimal("150"),
        maximum_turnover_notional=Decimal("1000"),
    )


def test_clean_market_data_and_risk_context_are_ready() -> None:
    quality = validate_bar_series(
        bars(),
        now=NOW,
        policy=MarketDataPolicy(maximum_last_bar_age=timedelta(seconds=5)),
    )
    assert quality.ready
    decision = PreTradeRiskEngine(limits()).evaluate(
        order(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=RiskContext(
            price_timestamp=NOW - timedelta(seconds=2),
            decision_time=NOW,
            spread_bps=Decimal("5"),
            estimated_slippage_bps=Decimal("5"),
            daily_pnl=Decimal("10"),
            drawdown=Decimal("20"),
            turnover_notional=Decimal("100"),
        ),
    )
    assert decision.approved
    assert decision.reasons == ()


def test_market_data_quality_fails_closed_on_time_gap_jump_and_staleness() -> None:
    bad = [
        Bar("AAPL", NOW - timedelta(minutes=20), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=10), Decimal("200")),
    ]
    quality = validate_bar_series(
        bad,
        now=NOW,
        policy=MarketDataPolicy(
            maximum_last_bar_age=timedelta(minutes=2),
            maximum_gap=timedelta(minutes=5),
            maximum_jump_fraction=Decimal("0.25"),
        ),
    )
    assert not quality.ready
    assert "BAR_GAP_EXCEEDED" in quality.reasons
    assert "PRICE_JUMP_EXCEEDED" in quality.reasons
    assert "STALE_LAST_BAR" in quality.reasons


def test_operational_risk_context_blocks_unsafe_order() -> None:
    decision = PreTradeRiskEngine(limits()).evaluate(
        order(),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        context=RiskContext(
            price_timestamp=NOW - timedelta(seconds=30),
            decision_time=NOW,
            market_open=False,
            halted=True,
            spread_bps=Decimal("21"),
            estimated_slippage_bps=Decimal("26"),
            daily_pnl=Decimal("-100"),
            drawdown=Decimal("150"),
            turnover_notional=Decimal("950"),
        ),
    )
    assert not decision.approved
    assert set(decision.reasons) == {
        "DAILY_LOSS_LIMIT_REACHED",
        "DRAWDOWN_LIMIT_REACHED",
        "INSTRUMENT_HALTED",
        "MARKET_CLOSED",
        "SLIPPAGE_LIMIT_EXCEEDED",
        "SPREAD_LIMIT_EXCEEDED",
        "STALE_PRICE",
        "TURNOVER_LIMIT_EXCEEDED",
    }
