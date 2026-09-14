from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.paper_pipeline import MarketDataNotReady, PaperTradingPipeline, PlanningMode
from app.domain.trading import Bar
from app.marketdata.validation import MarketDataPolicy, validate_bar_series
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.momentum import LongOnlyMomentumStrategy

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)


def risk() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        )
    )


def stale_bars() -> list[Bar]:
    return [
        Bar("AAPL", OLD - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", OLD - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", OLD, Decimal("102")),
    ]


def stale_gapped_bars() -> list[Bar]:
    return [
        Bar("AAPL", OLD - timedelta(minutes=20), Decimal("100")),
        Bar("AAPL", OLD - timedelta(minutes=10), Decimal("101")),
        Bar("AAPL", OLD, Decimal("102")),
    ]


class MustNotRunStrategy:
    def __init__(self) -> None:
        self.calls = 0

    def target(self, bars):
        self.calls += 1
        raise AssertionError("strategy must not receive unqualified operational data")


def operational_pipeline(strategy) -> PaperTradingPipeline:
    return PaperTradingPipeline(
        strategy=strategy,
        ledger=PortfolioLedger(opening_cash=Decimal("10000")),
        risk=risk(),
        mode=PlanningMode.OPERATIONAL,
    )


def test_f04_operational_clock_is_required_and_bar_time_cannot_self_certify() -> None:
    strategy = MustNotRunStrategy()
    pipeline = operational_pipeline(strategy)
    with pytest.raises(ValueError, match="OPERATIONAL_DECISION_TIME_REQUIRED"):
        pipeline.plan(stale_bars())
    assert strategy.calls == 0

    with pytest.raises(MarketDataNotReady) as exc_info:
        pipeline.plan(stale_bars(), decision_time=NOW)
    assert exc_info.value.reasons == ("STALE_LAST_BAR",)
    assert strategy.calls == 0


def test_f16_stale_gapped_operational_data_is_rejected_before_strategy() -> None:
    strategy = MustNotRunStrategy()
    pipeline = operational_pipeline(strategy)
    with pytest.raises(MarketDataNotReady) as exc_info:
        pipeline.plan(stale_gapped_bars(), decision_time=NOW)
    assert set(exc_info.value.reasons) == {"BAR_GAP_EXCEEDED", "STALE_LAST_BAR"}
    assert strategy.calls == 0


def test_replay_mode_preserves_historical_determinism() -> None:
    pipeline = PaperTradingPipeline(
        strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1")),
        ledger=PortfolioLedger(opening_cash=Decimal("10000")),
        risk=risk(),
        mode=PlanningMode.REPLAY,
    )
    target, intent, decision = pipeline.plan(stale_bars())
    assert target.generated_at == OLD
    assert intent is not None
    assert decision is not None and decision.approved


def test_f14_naive_final_bar_returns_invalid_quality_instead_of_type_error() -> None:
    malformed = [
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("100")),
        Bar("AAPL", datetime(2026, 9, 12, 12, 0), Decimal("101")),
    ]
    quality = validate_bar_series(
        malformed,
        now=NOW,
        policy=MarketDataPolicy(maximum_last_bar_age=timedelta(minutes=2)),
    )
    assert not quality.ready
    assert quality.reasons == ("INVALID_BAR",)
    assert quality.last_timestamp == NOW - timedelta(minutes=1)


def test_all_invalid_bars_fail_closed_without_clock_comparison() -> None:
    malformed = [Bar("AAPL", datetime(2026, 9, 12, 12, 0), Decimal("101"))]
    quality = validate_bar_series(malformed, now=NOW)
    assert not quality.ready
    assert set(quality.reasons) == {"INVALID_BAR", "NO_VALID_BARS"}
    assert quality.last_timestamp is None
