from datetime import datetime, timezone
from decimal import Decimal

from app.domain.trading import OrderIntent, Side
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskLimits

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 17, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="advanced-risk",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="risk-validation",
    )


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_order_notional=Decimal("2000"),
        maximum_symbol_notional=Decimal("5000"),
        maximum_gross_notional=Decimal("10000"),
        maximum_liquidity_participation_fraction=Decimal("0.10"),
        maximum_position_fraction_of_equity=Decimal("0.20"),
        maximum_sector_fraction_of_equity=Decimal("0.30"),
        maximum_annualized_volatility=Decimal("0.50"),
    )


def test_advanced_risk_context_can_pass_all_capacity_checks() -> None:
    result = PreTradeRiskEngine(limits()).evaluate(
        intent(),
        current_symbol_notional=Decimal("500"),
        current_gross_notional=Decimal("2000"),
        context=RiskContext(
            price_timestamp=NOW,
            decision_time=NOW,
            average_daily_dollar_volume=Decimal("50000"),
            portfolio_equity=Decimal("10000"),
            sector_notional=Decimal("1000"),
            annualized_volatility=Decimal("0.20"),
        ),
    )
    assert result.approved
    assert result.reasons == ()


def test_liquidity_concentration_and_volatility_breaches_are_all_reported() -> None:
    result = PreTradeRiskEngine(limits()).evaluate(
        intent(),
        current_symbol_notional=Decimal("1500"),
        current_gross_notional=Decimal("2000"),
        context=RiskContext(
            price_timestamp=NOW,
            decision_time=NOW,
            average_daily_dollar_volume=Decimal("5000"),
            portfolio_equity=Decimal("10000"),
            sector_notional=Decimal("2500"),
            annualized_volatility=Decimal("0.80"),
        ),
    )
    assert not result.approved
    assert set(result.reasons) == {
        "LIQUIDITY_PARTICIPATION_EXCEEDED",
        "POSITION_CONCENTRATION_EXCEEDED",
        "SECTOR_CONCENTRATION_EXCEEDED",
        "VOLATILITY_LIMIT_EXCEEDED",
    }
