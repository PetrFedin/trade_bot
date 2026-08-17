from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from tools.audit_bybit_crypto_position_selection import audit_position_selection


def _bar(symbol: str, index: int, *, slope: str, start: datetime) -> BybitKlineBar:
    close = Decimal("100") + Decimal(slope) * Decimal(index)
    return BybitKlineBar(
        symbol=symbol,
        start_time=start + timedelta(minutes=5 * index),
        open=close - Decimal(slope) * Decimal("0.10"),
        high=close + Decimal("0.60"),
        low=close - Decimal("0.60"),
        close=close,
        volume=Decimal("10000"),
        turnover=Decimal("2000000"),
    )


def _acquisition(count: int = 90) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, slope in (
        ("BTCUSDT", "0.70"),
        ("ETHUSDT", "0.45"),
        ("SOLUSDT", "-0.55"),
    ):
        bars.extend(_bar(symbol, index, slope=slope, start=start) for index in range(count))
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        minimum_average_turnover_usdt=Decimal("1000"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0.10"),
        maximum_one_bar_atr_multiple=Decimal("5"),
        risk_fraction_per_trade=Decimal("0.01"),
        maximum_notional_to_equity=Decimal("2"),
        expected_move_atr_multiple=Decimal("10"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        maximum_concurrent_positions=2,
    )


def test_selection_audit_compares_same_eligible_plans_without_pnl_claim() -> None:
    report = audit_position_selection(
        _acquisition(),
        equity_usdt=Decimal("1000"),
        config=_config(),
        maximum_positions=2,
    )

    assert report["qualification"] == "BYBIT_CRYPTO_POSITION_SELECTION_AUDIT"
    assert report["decision_count_with_eligible_plan"] > 0
    assert report["comparable_decision_count"] > 0
    assert report["top_set_divergence_fraction"] is not None
    assert report["top_order_divergence_fraction"] is not None
    assert report["current_average_expected_net_r"] is not None
    assert report["economic_average_expected_net_r"] is not None
    assert report["realized_pnl_compared"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
