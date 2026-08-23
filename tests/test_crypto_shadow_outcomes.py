from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    evaluate_crypto_signal,
)
from app.strategy.crypto_shadow_outcomes import (
    CryptoShadowSeed,
    CryptoShadowSourceCandidate,
    evaluate_crypto_shadow_outcome,
    reconstruct_crypto_shadow_seed,
)

_DECISION = datetime(2026, 8, 23, 12, tzinfo=UTC)
_AVAILABLE = _DECISION + timedelta(minutes=5)


def _seed(*, side: str = "LONG") -> CryptoShadowSeed:
    if side == "LONG":
        stop = Decimal("99")
        target = Decimal("102")
    else:
        stop = Decimal("101")
        target = Decimal("98")
    return CryptoShadowSeed(
        source_snapshot_id="a" * 64,
        source_evidence_rank=1,
        source_market_rank=2,
        source_qualification_state="QUALIFIED_POSITIVE_EVIDENCE",
        symbol="BTCUSDT",
        side=side,
        decision_bar_start_at=_DECISION.isoformat(),
        signal_available_at=_AVAILABLE.isoformat(),
        entry_price=Decimal("100"),
        stop_price=stop,
        target_price=target,
        planned_notional_usdt=Decimal("1000"),
        risk_budget_usdt=Decimal("10"),
        estimated_round_trip_cost_usdt=Decimal("2"),
        target_net_profit_usd=Decimal("20"),
        signal_quality_score=Decimal("2"),
    )


def _bar(
    start: datetime,
    *,
    opened: str = "100",
    high: str = "100.5",
    low: str = "99.5",
    close: str = "100",
) -> BybitKlineBar:
    return BybitKlineBar(
        symbol="BTCUSDT",
        start_time=start,
        open=Decimal(opened),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
    )


def test_shadow_outcome_ignores_decision_bar_and_future_uncompleted_bar() -> None:
    seed = _seed()
    bars = (
        _bar(_DECISION, high="110", low="90", close="105"),
        _bar(_AVAILABLE, close="100.5"),
        _bar(_AVAILABLE + timedelta(minutes=5), high="102.5", close="102"),
        _bar(_AVAILABLE + timedelta(minutes=10), close="101"),
        _bar(
            _AVAILABLE + timedelta(minutes=15),
            high="105",
            low="95",
            close="96",
        ),
    )
    outcome = evaluate_crypto_shadow_outcome(
        seed,
        bars,
        observed_through=_AVAILABLE + timedelta(minutes=15),
    )

    assert outcome.completed_bar_count == 3
    assert outcome.first_touch_state == "TARGET_FIRST"
    assert outcome.target_hit_at == (_AVAILABLE + timedelta(minutes=5)).isoformat()
    assert outcome.stop_hit_at is None
    assert outcome.first_touch_modeled_net_pnl_usdt == Decimal("20")
    assert outcome.horizons[0].complete is True
    assert outcome.horizons[0].close_time == (
        _AVAILABLE + timedelta(minutes=15)
    ).isoformat()
    assert outcome.horizons[1].complete is False
    assert outcome.horizons[2].complete is False
    assert outcome.final is False
    assert outcome.trade_actionable is False
    assert outcome.bybit_live_order_routing_allowed is False


def test_shadow_outcome_marks_same_bar_target_stop_order_as_ambiguous() -> None:
    seed = _seed()
    outcome = evaluate_crypto_shadow_outcome(
        seed,
        (_bar(_AVAILABLE, high="103", low="98", close="100"),),
        observed_through=_AVAILABLE + timedelta(minutes=5),
    )

    assert outcome.first_touch_state == "AMBIGUOUS_SAME_BAR"
    assert outcome.target_hit_at == _AVAILABLE.isoformat()
    assert outcome.stop_hit_at == _AVAILABLE.isoformat()
    assert outcome.first_touch_modeled_net_pnl_usdt is None


def test_shadow_short_stop_first_and_directional_horizon_pnl() -> None:
    seed = _seed(side="SHORT")
    bars = (
        _bar(_AVAILABLE, high="101.5", low="99.5", close="101"),
        _bar(_AVAILABLE + timedelta(minutes=5), close="100"),
        _bar(_AVAILABLE + timedelta(minutes=10), close="98"),
    )
    outcome = evaluate_crypto_shadow_outcome(
        seed,
        bars,
        observed_through=_AVAILABLE + timedelta(minutes=15),
    )

    assert outcome.first_touch_state == "STOP_FIRST"
    assert outcome.stop_hit_at == _AVAILABLE.isoformat()
    assert outcome.first_touch_modeled_net_pnl_usdt == Decimal("-12")
    horizon = outcome.horizons[0]
    assert horizon.complete is True
    assert horizon.directional_return_fraction == Decimal("0.02")
    assert horizon.gross_pnl_usdt == Decimal("20.00")
    assert horizon.modeled_net_pnl_usdt == Decimal("18.00")


def test_shadow_horizon_requires_complete_contiguous_completed_bars() -> None:
    seed = _seed()
    bars = (
        _bar(_AVAILABLE),
        _bar(_AVAILABLE + timedelta(minutes=10)),
    )
    outcome = evaluate_crypto_shadow_outcome(
        seed,
        bars,
        observed_through=_AVAILABLE + timedelta(minutes=15),
    )

    assert outcome.horizons[0].complete is False
    assert outcome.horizons[0].modeled_net_pnl_usdt is None


def test_shadow_240m_becomes_final_only_after_full_out_of_sample_window() -> None:
    seed = _seed()
    bars = tuple(
        _bar(
            _AVAILABLE + timedelta(minutes=5 * index),
            high="101",
            low="99.5",
            close="100.5",
        )
        for index in range(48)
    )
    outcome = evaluate_crypto_shadow_outcome(
        seed,
        bars,
        observed_through=_AVAILABLE + timedelta(minutes=240),
    )

    assert [item.complete for item in outcome.horizons] == [True, True, True]
    assert outcome.first_touch_state == "NEITHER"
    assert outcome.final is True
    assert outcome.completed_bar_count == 48
    assert outcome.mfe_r == Decimal("1")
    assert outcome.mae_r == Decimal("-0.5")


def _signal_history() -> tuple[BybitKlineBar, ...]:
    start = _DECISION - timedelta(minutes=5 * 119)
    rows: list[BybitKlineBar] = []
    for index in range(120):
        timestamp = start + timedelta(minutes=5 * index)
        close = Decimal("100") + Decimal(index)
        opened = Decimal("99") + Decimal(index)
        rows.append(
            BybitKlineBar(
                symbol="BTCUSDT",
                start_time=timestamp,
                open=opened,
                high=close + Decimal("0.4"),
                low=opened - Decimal("0.4"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(rows)


def test_shadow_seed_reconstructs_only_from_fixed_strategy_source_decision() -> None:
    bars = _signal_history()
    config = CryptoPerpStrategyConfig()
    evaluation = evaluate_crypto_signal(bars, config)
    assert evaluation.eligible is True
    assert evaluation.signal is not None
    plan_evaluation = build_trade_plan(
        evaluation.signal,
        equity_usdt=Decimal("1000"),
        config=config,
    )
    assert plan_evaluation.eligible is True
    assert plan_evaluation.plan is not None
    plan = plan_evaluation.plan
    source = CryptoShadowSourceCandidate(
        source_snapshot_id="b" * 64,
        evidence_rank=3,
        market_rank=7,
        qualification_state="NO_SAMPLE_SUFFICIENT_EXACT_CELL",
        symbol="BTCUSDT",
        side=evaluation.signal.side.value,
        decision_time=evaluation.signal.decision_time,
        signal_quality_score=evaluation.signal.quality_score,
        planned_notional_usdt=plan.notional_usdt,
        risk_budget_usdt=plan.risk_budget_usdt,
        estimated_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
    )

    seed = reconstruct_crypto_shadow_seed(source, bars)

    assert seed.decision_bar_start_at == _DECISION.isoformat()
    assert seed.signal_available_at == _AVAILABLE.isoformat()
    assert seed.entry_price == evaluation.signal.reference_price
    assert seed.planned_notional_usdt == plan.notional_usdt
    assert seed.source_qualification_state == "NO_SAMPLE_SUFFICIENT_EXACT_CELL"
    assert seed.trade_actionable is False
    assert seed.demo_activation_allowed is False
    assert seed.live_activation_allowed is False

    with pytest.raises(ValueError, match="qualified fixed strategy"):
        reconstruct_crypto_shadow_seed(
            source,
            bars,
            strategy_config=CryptoPerpStrategyConfig(minimum_quality_score=Decimal("2")),
        )
