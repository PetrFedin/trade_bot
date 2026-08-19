from datetime import UTC, datetime
from decimal import Decimal

from app.marketdata.bybit_funding import BybitFundingRateRecord
from app.strategy.crypto_funding_impact import (
    CryptoFundingImpactStatus,
    CryptoFundingMarkSnapshot,
    calculate_trade_funding_impact,
)


def _trade(*, side: str = "LONG") -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "side": side,
        "entry_time": "2026-08-01T01:00:00+00:00",
        "exit_time": "2026-08-01T17:00:00+00:00",
        "quantity": 0.1,
        "net_pnl_usdt": 20.0,
    }


def _rate(hour: int, value: str) -> BybitFundingRateRecord:
    return BybitFundingRateRecord(
        symbol="BTCUSDT",
        funding_time=datetime(2026, 8, 1, hour, tzinfo=UTC),
        funding_rate=Decimal(value),
    )


def _mark(hour: int, price: str) -> CryptoFundingMarkSnapshot:
    return CryptoFundingMarkSnapshot(
        symbol="BTCUSDT",
        funding_time=datetime(2026, 8, 1, hour, tzinfo=UTC),
        mark_price=Decimal(price),
        source="EXACT_TEST_MARK_SNAPSHOT",
    )


def test_positive_funding_rate_debits_long_and_credits_short() -> None:
    rates = (_rate(8, "0.0001"), _rate(16, "0.0002"))
    marks = (_mark(8, "100000"), _mark(16, "110000"))

    long = calculate_trade_funding_impact(
        _trade(side="LONG"),
        funding_rates=rates,
        mark_snapshots=marks,
    )
    short = calculate_trade_funding_impact(
        _trade(side="SHORT"),
        funding_rates=rates,
        mark_snapshots=marks,
    )

    expected = Decimal("0.1") * Decimal("100000") * Decimal("0.0001")
    expected += Decimal("0.1") * Decimal("110000") * Decimal("0.0002")
    assert long.status is CryptoFundingImpactStatus.COMPLETE
    assert long.funding_pnl_usdt == -expected
    assert long.trade_net_after_funding_usdt == Decimal("20") - expected
    assert short.funding_pnl_usdt == expected
    assert short.trade_net_after_funding_usdt == Decimal("20") + expected
    assert long.event_count == 2
    assert long.accounting_overlay_only is True
    assert long.funding_feedback_into_position_sizing is False
    assert long.funding_feedback_into_session_risk is False
    assert long.strategy_promotion_allowed is False
    assert long.live_activation_allowed is False


def test_missing_mark_price_refuses_to_invent_funding_pnl() -> None:
    result = calculate_trade_funding_impact(
        _trade(),
        funding_rates=(_rate(8, "0.0001"),),
        mark_snapshots=(),
    )

    assert result.status is CryptoFundingImpactStatus.MISSING_MARK_PRICE
    assert result.funding_pnl_usdt is None
    assert result.trade_net_after_funding_usdt is None
    assert result.missing_mark_times == (datetime(2026, 8, 1, 8, tzinfo=UTC),)


def test_funding_event_exactly_on_entry_boundary_is_left_ambiguous() -> None:
    rate = BybitFundingRateRecord(
        symbol="BTCUSDT",
        funding_time=datetime(2026, 8, 1, 1, tzinfo=UTC),
        funding_rate=Decimal("0.0001"),
    )
    result = calculate_trade_funding_impact(
        _trade(),
        funding_rates=(rate,),
        mark_snapshots=(_mark(1, "100000"),),
    )

    assert result.status is CryptoFundingImpactStatus.FUNDING_BOUNDARY_AMBIGUOUS
    assert result.funding_pnl_usdt is None
    assert result.trade_net_after_funding_usdt is None
    assert result.ambiguous_boundary_times == (datetime(2026, 8, 1, 1, tzinfo=UTC),)


def test_trade_without_funding_event_remains_complete_with_zero_funding() -> None:
    result = calculate_trade_funding_impact(
        _trade(),
        funding_rates=(_rate(0, "0.0001"),),
        mark_snapshots=(),
    )

    assert result.status is CryptoFundingImpactStatus.COMPLETE
    assert result.funding_pnl_usdt == Decimal("0")
    assert result.trade_net_after_funding_usdt == Decimal("20.0")
    assert result.event_count == 0
