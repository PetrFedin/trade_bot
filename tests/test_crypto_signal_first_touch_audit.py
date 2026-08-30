from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    CryptoTradePlan,
)
from app.strategy.crypto_signal_first_touch_audit import (
    CryptoModeledEntryLevels,
    CryptoSignalFirstTouchPolicy,
    audit_crypto_plan_eligible_first_touch,
    evaluate_crypto_signal_first_touch,
    model_crypto_signal_entry_levels,
)
from tools import replay_bybit_crypto as replay_core


def _signal(side: CryptoSide = CryptoSide.LONG) -> CryptoSignal:
    return CryptoSignal(
        symbol="BTCUSDT",
        side=side,
        reference_price=Decimal("100"),
        momentum=Decimal("0.02") if side is CryptoSide.LONG else Decimal("-0.02"),
        atr_fraction=Decimal("0.01"),
        fast_ema=Decimal("101") if side is CryptoSide.LONG else Decimal("99"),
        slow_ema=Decimal("99") if side is CryptoSide.LONG else Decimal("101"),
        breakout_strength_atr=Decimal("0.5"),
        one_bar_atr_multiple=Decimal("0.5"),
        average_turnover_usdt=Decimal("1000000"),
        quality_score=Decimal("3"),
        decision_time="2026-08-01T00:00:00+00:00",
    )


def _plan(side: CryptoSide = CryptoSide.LONG) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-01T00:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("10"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal("1.6"),
        estimated_stop_loss_after_cost_usdt=Decimal("11.6"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0216"),
        expected_move_fraction=Decimal("0.03"),
        expected_net_edge_usd=Decimal("28.4"),
        quality_score=Decimal("3"),
    )


def _bar(
    symbol: str,
    index: int,
    *,
    slope: str,
    start: datetime,
    spread: str = "0.8",
) -> BybitKlineBar:
    close = Decimal("100") + Decimal(slope) * Decimal(index)
    width = Decimal(spread)
    return BybitKlineBar(
        symbol=symbol,
        start_time=start + timedelta(minutes=5 * index),
        open=close - Decimal(slope) * Decimal("0.1"),
        high=close + width,
        low=close - width,
        close=close,
        volume=Decimal("10000"),
        turnover=Decimal("2000000"),
    )


def _assert_entry_level_parity(side: CryptoSide) -> None:
    config = CryptoPerpStrategyConfig()
    bar = _bar(
        "BTCUSDT",
        1,
        slope="0.2",
        start=datetime(2026, 8, 1, tzinfo=UTC),
    )
    signal = _signal(side)
    plan = _plan(side)
    levels = model_crypto_signal_entry_levels(
        signal,
        plan,
        raw_next_open_price=bar.open,
        config=config,
    )
    pending = replay_core._PendingEntry(plan=plan, signal=signal)
    canonical = replay_core._open_position(pending, bar=bar, config=config)

    assert levels.entry_execution_price == canonical.entry_price
    assert levels.quantity == canonical.quantity
    assert levels.entry_fee_usdt == canonical.entry_fee
    assert levels.hard_stop_raw_price == canonical.protection.active_stop_price
    assert levels.target_raw_price == canonical.target_trigger_price
    assert levels.risk_price_distance == canonical.risk_price_distance


def test_modeled_long_entry_levels_match_canonical_replay_open_position() -> None:
    _assert_entry_level_parity(CryptoSide.LONG)


def test_modeled_short_entry_levels_match_canonical_replay_open_position() -> None:
    _assert_entry_level_parity(CryptoSide.SHORT)


def test_first_touch_keeps_same_bar_target_stop_as_ambiguous() -> None:
    levels = CryptoModeledEntryLevels(
        entry_execution_price=Decimal("100"),
        quantity=Decimal("10"),
        entry_fee_usdt=Decimal("0.6"),
        hard_stop_raw_price=Decimal("99"),
        target_raw_price=Decimal("102"),
        risk_price_distance=Decimal("1"),
    )
    bar = BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("98"),
        close=Decimal("101"),
        volume=Decimal("1000"),
        turnover=Decimal("100000"),
    )

    state, timestamp = evaluate_crypto_signal_first_touch(
        side=CryptoSide.LONG,
        levels=levels,
        bars=(bar,),
        complete=True,
    )

    assert state == "AMBIGUOUS_SAME_BAR"
    assert timestamp == bar.start_time.isoformat()


def test_audit_exposes_independent_episodes_without_promoting_strategy() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, slope in (
        ("BTCUSDT", "0.30"),
        ("ETHUSDT", "0.25"),
        ("SOLUSDT", "-0.28"),
    ):
        bars.extend(
            _bar(symbol, index, slope=slope, start=start, spread="0.25")
            for index in range(150)
        )
    acquisition = BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )
    config = CryptoPerpStrategyConfig(
        minimum_average_turnover_usdt=Decimal("1000"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0.10"),
        maximum_one_bar_atr_multiple=Decimal("5"),
        risk_fraction_per_trade=Decimal("0.01"),
        maximum_notional_to_equity=Decimal("2"),
        expected_move_atr_multiple=Decimal("10"),
        target_net_profit_usd=Decimal("20"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        maximum_concurrent_positions=2,
    )
    report = audit_crypto_plan_eligible_first_touch(
        acquisition,
        strategy_config=config,
        policy=CryptoSignalFirstTouchPolicy(
            minimum_pattern_observations=2,
            sample_sufficient_observations=5,
            minimum_cross_symbol_count=2,
            minimum_distinct_days=1,
        ),
    )

    assert report["audit"] == "BYBIT_CRYPTO_PLAN_ELIGIBLE_FIRST_TOUCH_V2"
    assert report["plan_eligible_signal_count"] > 0
    assert report["aggregate"]["complete_count"] > 0
    assert 0 < report["independent_episode_count"] <= report["plan_eligible_signal_count"]
    assert len(report["outcome_rows"]) == report["plan_eligible_signal_count"]
    assert len(report["episode_outcome_rows"]) == report["independent_episode_count"]
    assert report["perfect_target_first_pattern_count"] == report[
        "episode_perfect_target_first_pattern_count"
    ]
    assert report["pattern_thresholds_fitted_to_outcomes"] is False
    assert report["episode_definition_fitted_to_outcomes"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
