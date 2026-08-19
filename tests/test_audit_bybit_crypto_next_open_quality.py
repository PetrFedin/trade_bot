from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from tools.audit_bybit_crypto_next_open_quality import audit_next_open_quality


def _bar(symbol: str, index: int, *, slope: str, start: datetime) -> BybitKlineBar:
    close = Decimal("100") + Decimal(slope) * Decimal(index)
    return BybitKlineBar(
        symbol=symbol,
        start_time=start + timedelta(minutes=5 * index),
        open=close - Decimal(slope) * Decimal("0.20"),
        high=close + Decimal("0.60"),
        low=close - Decimal("0.60"),
        close=close,
        volume=Decimal("10000"),
        turnover=Decimal("2000000"),
    )


def _acquisition(count: int = 90) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, slope in (("BTCUSDT", "0.70"), ("ETHUSDT", "-0.55")):
        bars.extend(_bar(symbol, index, slope=slope, start=start) for index in range(count))
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1},
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


def test_next_open_audit_reports_gap_distribution_without_selecting_threshold() -> None:
    report = audit_next_open_quality(
        _acquisition(),
        equity_usdt=Decimal("1000"),
        config=_config(),
    )

    assert report["qualification"] == "BYBIT_CRYPTO_NEXT_OPEN_QUALITY_AUDIT"
    plan_count = report["eligible_trade_plan_count"]
    assert plan_count > 0
    assert report["absolute_gap_atr"]["count"] == plan_count
    assert report["absolute_gap_atr"]["p50"] is not None
    assert report["absolute_gap_atr"]["p90"] is not None
    assert report["absolute_gap_atr"]["p95"] is not None
    quantity = report["execution_quantity_retention_fraction"]
    assert quantity["count"] == plan_count
    assert quantity["min"] is not None
    assert quantity["p05"] is not None
    assert quantity["p10"] is not None
    assert quantity["p50"] is not None
    assert quantity["max"] <= 1.0
    assert report["execution_expected_edge_ratio_to_planned"]["count"] == plan_count
    assert report["execution_risk_budget_utilization_fraction"]["count"] == plan_count
    assert report["execution_quantity_below_95pct_count"] <= plan_count
    assert report["execution_quantity_below_90pct_count"] <= plan_count
    assert report["gap_threshold_selected"] is False
    assert report["execution_size_threshold_selected"] is False
    assert report["automatic_execution_gate_activation_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
