from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from tools.qualify_bybit_crypto_walk_forward import CryptoWalkForwardPolicy
from tools.run_bybit_crypto_walk_forward_archive import acquire_archive_and_run_walk_forward


class _FakeArchiveAcquisition:
    def __init__(self, klines: BybitKlineAcquisition) -> None:
        self.klines = klines
        self.validated = False

    def validate(
        self,
        *,
        requested_symbols: tuple[str, ...],
        minimum_bars: int,
    ) -> None:
        assert requested_symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        assert minimum_bars == 25
        self.validated = True


class _FakeArchiveClient:
    def __init__(self, acquisition: _FakeArchiveAcquisition) -> None:
        self.acquisition = acquisition
        self.requested_dates = 0

    def fetch_klines(
        self,
        *,
        symbols: tuple[str, ...],
        dates: tuple[object, ...],
        interval_minutes: int,
    ) -> _FakeArchiveAcquisition:
        assert symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        assert interval_minutes == 5
        self.requested_dates = len(dates)
        return self.acquisition


def _klines(days: int = 4, bars_per_day: int = 60) -> BybitKlineAcquisition:
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


def _policy() -> CryptoWalkForwardPolicy:
    return CryptoWalkForwardPolicy(
        fold_days=1,
        minimum_folds=4,
        minimum_total_closed_trades=1,
        minimum_positive_fold_fraction=Decimal("0"),
        minimum_aggregate_profit_factor=Decimal("0.01"),
        maximum_worst_fold_drawdown_pct=Decimal("100"),
        require_zero_risk_budget_breaches=False,
    )


def test_archive_wrapper_is_validated_before_walk_forward() -> None:
    acquisition = _FakeArchiveAcquisition(_klines())
    client = _FakeArchiveClient(acquisition)

    report = acquire_archive_and_run_walk_forward(
        symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        lookback_days=4,
        opening_equity_usdt=Decimal("1000"),
        policy=_policy(),
        client=client,
    )

    assert acquisition.validated is True
    assert client.requested_dates == 4
    assert report["fold_count"] == 4
    assert report["source"] == "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M"
    assert report["archive_completed_utc_days_only"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_observation_allowed"] is False
    assert report["live_promotion_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_archive_runner_refuses_shorter_than_required_window() -> None:
    acquisition = _FakeArchiveAcquisition(_klines())
    client = _FakeArchiveClient(acquisition)
    with pytest.raises(ValueError, match="at least 4"):
        acquire_archive_and_run_walk_forward(
            symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
            lookback_days=3,
            policy=_policy(),
            client=client,
        )
