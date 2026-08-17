from decimal import Decimal

from app.strategy.crypto_perp import CryptoSide, CryptoSignal, CryptoTradePlan
from app.strategy.crypto_position_selection import (
    CryptoPositionCandidate,
    rank_crypto_position_candidates,
    select_crypto_positions,
)


def _candidate(
    symbol: str,
    *,
    expected_edge: str,
    risk_budget: str,
    quality: str,
    cost: str,
) -> CryptoPositionCandidate:
    signal = CryptoSignal(
        symbol=symbol,
        side=CryptoSide.LONG,
        reference_price=Decimal("100"),
        momentum=Decimal("0.01"),
        atr_fraction=Decimal("0.01"),
        fast_ema=Decimal("101"),
        slow_ema=Decimal("100"),
        breakout_strength_atr=Decimal("1"),
        one_bar_atr_multiple=Decimal("0.5"),
        average_turnover_usdt=Decimal("1000000"),
        quality_score=Decimal(quality),
        decision_time="2026-08-17T10:00:00+00:00",
    )
    plan = CryptoTradePlan(
        symbol=symbol,
        side=CryptoSide.LONG,
        decision_time=signal.decision_time,
        reference_price=Decimal("100"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("10"),
        risk_budget_usdt=Decimal(risk_budget),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal(cost),
        estimated_stop_loss_after_cost_usdt=Decimal(risk_budget),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.03"),
        expected_move_fraction=Decimal("0.05"),
        expected_net_edge_usd=Decimal(expected_edge),
        quality_score=Decimal(quality),
    )
    return CryptoPositionCandidate(signal=signal, plan=plan)


def test_position_ranking_prefers_expected_net_r_before_raw_signal_quality() -> None:
    high_quality_lower_r = _candidate(
        "BTCUSDT",
        expected_edge="25",
        risk_budget="10",
        quality="5",
        cost="2",
    )
    lower_quality_higher_r = _candidate(
        "ETHUSDT",
        expected_edge="30",
        risk_budget="8",
        quality="3",
        cost="2",
    )

    ranked = rank_crypto_position_candidates(
        (high_quality_lower_r, lower_quality_higher_r)
    )

    assert ranked[0].plan.symbol == "ETHUSDT"
    assert ranked[0].expected_net_r == Decimal("3.75")
    assert ranked[1].expected_net_r == Decimal("2.5")


def test_position_ranking_uses_lower_cost_burden_only_after_edge_and_quality_ties() -> None:
    a = _candidate(
        "BTCUSDT",
        expected_edge="30",
        risk_budget="10",
        quality="3",
        cost="3",
    )
    b = _candidate(
        "ETHUSDT",
        expected_edge="30",
        risk_budget="10",
        quality="3",
        cost="2",
    )

    ranked = rank_crypto_position_candidates((a, b))

    assert ranked[0].plan.symbol == "ETHUSDT"
    assert ranked[0].cost_to_target_fraction == Decimal("0.1")


def test_selection_is_shadow_only_and_bounded() -> None:
    selection = select_crypto_positions(
        (
            _candidate("BTCUSDT", expected_edge="30", risk_budget="10", quality="3", cost="2"),
            _candidate("ETHUSDT", expected_edge="40", risk_budget="10", quality="4", cost="2"),
            _candidate("SOLUSDT", expected_edge="35", risk_budget="10", quality="2", cost="2"),
        ),
        maximum_positions=2,
    )

    assert len(selection.selected) == 2
    assert len(selection.rejected) == 1
    assert selection.ranking_contract == (
        "expected_net_r_desc",
        "expected_net_edge_usd_desc",
        "quality_score_desc",
        "cost_to_target_fraction_asc",
        "symbol_asc",
    )
    assert selection.shadow_only is True
    assert selection.demo_activation_allowed is False
    assert selection.live_activation_allowed is False
