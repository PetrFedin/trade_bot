from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from tools.qualify_bybit_crypto_walk_forward import CryptoWalkForwardPolicy, run_crypto_walk_forward


def _acquisition(days: int = 4, bars_per_day: int = 60) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars: list[BybitKlineBar] = []
    for day in range(days):
        day_start = start + timedelta(days=day)
        for symbol, direction in (("BTCUSDT", 1), ("ETHUSDT", 1), ("SOLUSDT", -1)):
            base = Decimal("100") + Decimal(day * 5)
            for index in range(bars_per_day):
                close = base + Decimal(direction) * Decimal("0.7") * Decimal(index)
                bars.append(
                    BybitKlineBar(
                        symbol=symbol,
                        start_time=day_start + timedelta(minutes=5 * index),
                        open=close - Decimal(direction) * Decimal("0.10"),
                        high=close + Decimal("0.50"),
                        low=close - Decimal("0.50"),
                        close=close,
                        volume=Decimal("10000"),
                        turnover=Decimal("2000000"),
                    )
                )
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )


def test_walk_forward_reports_long_short_without_authorizing_filter() -> None:
    report = run_crypto_walk_forward(
        _acquisition(),
        opening_equity_usdt=Decimal("1000"),
        policy=CryptoWalkForwardPolicy(
            fold_days=1,
            minimum_folds=4,
            minimum_total_closed_trades=1,
            minimum_positive_fold_fraction=Decimal("0"),
            minimum_aggregate_profit_factor=Decimal("0.01"),
            maximum_worst_fold_drawdown_pct=Decimal("100"),
            require_zero_risk_budget_breaches=False,
        ),
    )

    decisions = report["candidate_decisions"]
    diagnostics = report["candidate_side_diagnostics"]
    assert set(diagnostics) == set(decisions)
    baseline = diagnostics["CONDITIONAL_1_5X"]
    assert set(baseline) == {"LONG", "SHORT"}
    assert (
        baseline["LONG"]["closed_trade_count"]
        + baseline["SHORT"]["closed_trade_count"]
        == decisions["CONDITIONAL_1_5X"]["total_closed_trades"]
    )
    for side in baseline.values():
        assert side["fold_count"] == 4
        assert len(side["fold_net_pnl_usdt"]) == 4
        assert side["directional_filter_selection_allowed"] is False
    assert report["directional_filter_selection_allowed"] is False
    assert set(
        report["folds"][0]["candidate_metrics"]["CONDITIONAL_1_5X"]["side_metrics"]
    ) == {"LONG", "SHORT"}
