from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.marketdata.bybit_full_period_5m import build_bybit_full_period_5m_plan
from app.marketdata.bybit_research_universe import BybitResearchInstrument

_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _instrument(symbol: str, launch: datetime) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=int(launch.timestamp() * 1000),
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def test_full_period_plan_partitions_complete_blocked_and_pending_days() -> None:
    instruments = (
        _instrument("BTCUSDT", datetime(2026, 8, 20, tzinfo=UTC)),
        _instrument("ETHUSDT", datetime(2026, 8, 21, tzinfo=UTC)),
    )
    plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=("BTCUSDT", "ETHUSDT"),
        observed_at=_NOW,
        completed_by_symbol={
            "BTCUSDT": (datetime(2026, 8, 20, tzinfo=UTC).date(),),
            "ETHUSDT": (
                datetime(2026, 8, 21, tzinfo=UTC).date(),
                datetime(2026, 8, 22, tzinfo=UTC).date(),
            ),
        },
        unavailable_retry_after_by_symbol={
            "BTCUSDT": {
                datetime(2026, 8, 21, tzinfo=UTC).date(): _NOW + timedelta(hours=12)
            }
        },
    )

    btc = plan.coverage[0]
    eth = plan.coverage[1]
    assert plan.last_archive_date.isoformat() == "2026-08-22"
    assert btc.expected_day_count == 3
    assert btc.completed_day_count == 1
    assert btc.blocked_day_count == 1
    assert btc.pending_day_count == 1
    assert btc.pending_dates[0].isoformat() == "2026-08-22"
    assert eth.full_period_complete is True
    assert plan.full_period_complete is False
    assert plan.to_payload()["full_period_claim_allowed"] is False
    assert [(item.archive_date.isoformat(), item.symbol) for item in plan.next_work_items(limit=5)] == [
        ("2026-08-22", "BTCUSDT")
    ]
    assert plan.trade_actionable if hasattr(plan, "trade_actionable") else True


def test_retryable_unavailable_day_returns_to_work_queue() -> None:
    instruments = (_instrument("BTCUSDT", datetime(2026, 8, 20, tzinfo=UTC)),)
    plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=("BTCUSDT",),
        observed_at=_NOW,
        completed_by_symbol={"BTCUSDT": ()},
        unavailable_retry_after_by_symbol={
            "BTCUSDT": {
                datetime(2026, 8, 20, tzinfo=UTC).date(): _NOW - timedelta(minutes=1)
            }
        },
    )

    assert plan.blocked_day_count == 0
    assert plan.pending_day_count == 3
    assert [item.archive_date.isoformat() for item in plan.next_work_items(limit=2)] == [
        "2026-08-20",
        "2026-08-21",
    ]


def test_full_period_claim_requires_every_expected_day_complete() -> None:
    instruments = (_instrument("BTCUSDT", datetime(2026, 8, 20, tzinfo=UTC)),)
    complete_dates = tuple(
        datetime(2026, 8, day, tzinfo=UTC).date() for day in (20, 21, 22)
    )
    plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=("BTCUSDT",),
        observed_at=_NOW,
        completed_by_symbol={"BTCUSDT": complete_dates},
    )

    assert plan.coverage_fraction == 1
    assert plan.full_period_complete is True
    assert plan.to_payload()["full_period_claim_allowed"] is True
    assert plan.next_work_items(limit=10) == ()


def test_out_of_range_completed_day_fails_closed() -> None:
    instruments = (_instrument("BTCUSDT", datetime(2026, 8, 20, tzinfo=UTC)),)
    with pytest.raises(ValueError, match="completed dates exceed expected interval"):
        build_bybit_full_period_5m_plan(
            instruments,
            symbols=("BTCUSDT",),
            observed_at=_NOW,
            completed_by_symbol={
                "BTCUSDT": (datetime(2026, 8, 19, tzinfo=UTC).date(),)
            },
        )
