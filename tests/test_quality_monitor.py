from decimal import Decimal

from app.strategy.quality_monitor import (
    StrategyQualityStatus,
    TradeQualityMonitorPolicy,
    TradeQualityObservation,
    compute_trade_quality_window,
    evaluate_strategy_quality_gate,
)


def policy(*, window: int = 5, minimum: int = 3) -> TradeQualityMonitorPolicy:
    return TradeQualityMonitorPolicy(
        window_trades=window,
        minimum_observations=minimum,
        minimum_profit_factor=Decimal("1.2"),
        minimum_profit_preservation_rate=Decimal("0.50"),
        minimum_average_mfe_capture_ratio=Decimal("0.10"),
        maximum_hard_stop_fraction=Decimal("0.50"),
        maximum_consecutive_losses=3,
        allow_entries_when_insufficient_data=False,
    )


def observation(
    pnl: str,
    *,
    mfe: str = "0.01",
    capture: str | None = "0.20",
    reason: str = "TAKE_PROFIT",
) -> TradeQualityObservation:
    return TradeQualityObservation(
        net_pnl=Decimal(pnl),
        maximum_favorable_excursion_fraction=Decimal(mfe),
        mfe_capture_ratio=None if capture is None else Decimal(capture),
        exit_reason=reason,
    )


def test_quality_gate_fails_closed_for_new_entries_until_minimum_sample() -> None:
    result = evaluate_strategy_quality_gate(
        (observation("2"), observation("1")),
        policy=policy(minimum=3),
    )
    assert result.status is StrategyQualityStatus.INSUFFICIENT_DATA
    assert result.allow_new_entries is False
    assert result.allow_exits is True
    assert result.reasons == ("INSUFFICIENT_OBSERVATIONS",)


def test_healthy_recent_trade_quality_keeps_entries_enabled() -> None:
    result = evaluate_strategy_quality_gate(
        (
            observation("10", mfe="0.02", capture="0.50"),
            observation("5", mfe="0.01", capture="0.40", reason="TRAILING_STOP"),
            observation("-2", mfe="0.005", capture="-0.20", reason="INTRABAR_HARD_STOP"),
            observation("4", mfe="0.015", capture="0.30", reason="INTRABAR_PROFIT_PROTECTION"),
        ),
        policy=policy(),
    )
    assert result.status is StrategyQualityStatus.HEALTHY
    assert result.allow_new_entries is True
    assert result.allow_exits is True
    assert result.reasons == ()
    assert result.metrics.profit_factor == Decimal("9.5")
    assert result.metrics.profit_preservation_rate == Decimal("0.75")
    assert result.metrics.hard_stop_fraction == Decimal("0.25")


def test_degraded_quality_pauses_entries_without_blocking_exits() -> None:
    result = evaluate_strategy_quality_gate(
        (
            observation("2", mfe="0.02", capture="0.20"),
            observation("-5", mfe="0.02", capture="-0.40", reason="INTRABAR_HARD_STOP"),
            observation("-4", mfe="0.01", capture="-0.50", reason="INTRABAR_HARD_STOP"),
            observation("-3", mfe="0.01", capture="-0.60", reason="INTRABAR_HARD_STOP"),
        ),
        policy=policy(),
    )
    assert result.status is StrategyQualityStatus.PAUSE_ENTRIES
    assert result.allow_new_entries is False
    assert result.allow_exits is True
    assert set(result.reasons) == {
        "PROFIT_FACTOR_BELOW_MINIMUM",
        "PROFIT_PRESERVATION_BELOW_MINIMUM",
        "MFE_CAPTURE_BELOW_MINIMUM",
        "HARD_STOP_FRACTION_ABOVE_MAXIMUM",
        "CONSECUTIVE_LOSS_LIMIT_REACHED",
    }


def test_monitor_uses_only_declared_recent_window() -> None:
    observations = (
        observation("-10", reason="INTRABAR_HARD_STOP"),
        observation("-10", reason="INTRABAR_HARD_STOP"),
        observation("5", capture="0.50"),
        observation("5", capture="0.50"),
        observation("5", capture="0.50"),
    )
    metrics = compute_trade_quality_window(observations, policy=policy(window=3, minimum=3))
    assert metrics.observation_count == 3
    assert metrics.winning_trades == 3
    assert metrics.losing_trades == 0
    assert metrics.total_pnl == Decimal("15")
    assert metrics.profit_factor is None
    assert metrics.current_consecutive_losses == 0
