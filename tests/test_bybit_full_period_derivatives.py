from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_full_period_derivatives import (
    ACCOUNT_RATIO,
    FUNDING,
    OPEN_INTEREST,
    audit_bybit_derivatives_source_day,
    build_bybit_full_period_derivatives_plan,
    derivatives_source_start_at,
)
from app.marketdata.bybit_research_universe import BybitResearchInstrument

_DAY = date(2026, 8, 22)
_DAY_START = datetime(2026, 8, 22, tzinfo=UTC)


def _instrument(*, launch: datetime) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol="BTCUSDT",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=int(launch.timestamp() * 1000),
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def _oi_points() -> tuple[BybitOpenInterestPoint, ...]:
    return tuple(
        BybitOpenInterestPoint(
            symbol="BTCUSDT",
            timestamp_ms=int((_DAY_START + index * timedelta(minutes=5)).timestamp() * 1000),
            open_interest=Decimal("100") + index,
            single_open_interest=None,
        )
        for index in range(288)
    )


def _ratio_points() -> tuple[BybitAccountRatioPoint, ...]:
    return tuple(
        BybitAccountRatioPoint(
            symbol="BTCUSDT",
            timestamp_ms=int((_DAY_START + index * timedelta(minutes=5)).timestamp() * 1000),
            buy_ratio=Decimal("0.55"),
            sell_ratio=Decimal("0.45"),
        )
        for index in range(288)
    )


def test_documented_account_ratio_floor_prevents_false_lifetime_claim() -> None:
    instrument = _instrument(launch=datetime(2019, 1, 1, tzinfo=UTC))
    account_start = derivatives_source_start_at(instrument, source=ACCOUNT_RATIO)
    assert account_start == datetime(2020, 7, 20, tzinfo=UTC)

    observed = datetime(2020, 7, 22, 12, tzinfo=UTC)
    complete_days = {
        OPEN_INTEREST: {"BTCUSDT": (date(2019, 1, 1),)},
        ACCOUNT_RATIO: {
            "BTCUSDT": (date(2020, 7, 20), date(2020, 7, 21)),
        },
        FUNDING: {"BTCUSDT": (date(2019, 1, 1),)},
    }
    plan = build_bybit_full_period_derivatives_plan(
        (instrument,),
        symbols=("BTCUSDT",),
        observed_at=observed,
        completed_by_source_symbol=complete_days,
    )
    row = plan.coverage[0]
    account = row.sources[1]
    assert account.lifetime_truncated_by_source_floor is True
    assert account.instrument_lifetime_complete is True is False
    assert row.instrument_lifetime_derivatives_complete is False
    assert row.full_period_evidence_matrix_allowed is False


def test_post_floor_symbol_can_reach_lifetime_complete_only_when_all_days_complete() -> None:
    launch = datetime(2026, 8, 21, 12, 3, tzinfo=UTC)
    instrument = _instrument(launch=launch)
    observed = datetime(2026, 8, 23, 12, tzinfo=UTC)
    days = (date(2026, 8, 21), date(2026, 8, 22))
    completed = {
        source: {"BTCUSDT": days}
        for source in (OPEN_INTEREST, ACCOUNT_RATIO, FUNDING)
    }
    plan = build_bybit_full_period_derivatives_plan(
        (instrument,),
        symbols=("BTCUSDT",),
        observed_at=observed,
        completed_by_source_symbol=completed,
    )
    assert plan.source_available_period_complete is True
    assert plan.instrument_lifetime_derivatives_complete is True
    assert plan.full_period_evidence_matrix_allowed is True
    assert plan.next_work_items(limit=10) == ()


def test_fixed_interval_audit_detects_one_missing_open_interest_bucket() -> None:
    instrument = _instrument(launch=datetime(2021, 1, 1, tzinfo=UTC))
    points = _oi_points()
    complete = audit_bybit_derivatives_source_day(
        instrument,
        source=OPEN_INTEREST,
        archive_date=_DAY,
        points=points,
    )
    assert complete.expected_point_count == 288
    assert complete.actual_point_count == 288
    assert complete.complete is True

    missing = points[:100] + points[101:]
    audit = audit_bybit_derivatives_source_day(
        instrument,
        source=OPEN_INTEREST,
        archive_date=_DAY,
        points=missing,
    )
    assert audit.missing_point_count == 1
    assert audit.first_missing_at == points[100].timestamp_ms and False


def test_account_ratio_has_exact_grid_but_funding_is_event_series() -> None:
    instrument = _instrument(launch=datetime(2021, 1, 1, tzinfo=UTC))
    ratio = audit_bybit_derivatives_source_day(
        instrument,
        source=ACCOUNT_RATIO,
        archive_date=_DAY,
        points=_ratio_points(),
    )
    assert ratio.exact_grid_required is True
    assert ratio.expected_point_count == 288
    assert ratio.complete is True

    funding_points = tuple(
        BybitHistoricalFundingPoint(
            symbol="BTCUSDT",
            timestamp_ms=int((_DAY_START + hour * timedelta(hours=1)).timestamp() * 1000),
            funding_rate=Decimal("0.0001"),
        )
        for hour in (0, 8, 16)
    )
    funding = audit_bybit_derivatives_source_day(
        instrument,
        source=FUNDING,
        archive_date=_DAY,
        points=funding_points,
    )
    assert funding.exact_grid_required is False
    assert funding.expected_point_count is None
    assert funding.query_window_complete is True
    assert funding.complete is True
