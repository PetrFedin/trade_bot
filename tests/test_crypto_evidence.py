from decimal import Decimal

from app.strategy.crypto_evidence import (
    CryptoEvidencePolicy,
    CryptoReplayEvidence,
    CryptoResearchPosture,
    evaluate_crypto_replay_evidence,
)


def _evidence(**overrides: object) -> CryptoReplayEvidence:
    values: dict[str, object] = {
        "target_net_profit_usd": Decimal("15"),
        "opening_equity_usdt": Decimal("1000"),
        "closed_trade_count": 40,
        "accepted_trade_plan_event_count": 45,
        "total_net_pnl_usdt": Decimal("120"),
        "profit_factor": Decimal("1.5"),
        "maximum_drawdown_pct": Decimal("3"),
        "fees_usdt": Decimal("20"),
        "risk_budget_breach_count": 0,
        "observed_days": Decimal("21"),
    }
    values.update(overrides)
    return CryptoReplayEvidence(**values)  # type: ignore[arg-type]


def test_zero_trade_plan_target_is_retune_not_demo_ready() -> None:
    decision = evaluate_crypto_replay_evidence(
        _evidence(
            accepted_trade_plan_event_count=0,
            closed_trade_count=0,
            total_net_pnl_usdt=Decimal("0"),
            profit_factor=None,
        )
    )

    assert decision.posture is CryptoResearchPosture.RETUNE_TARGET_FEASIBILITY
    assert decision.reasons == ("NO_TRADE_PLAN_MET_TARGET_NET_EDGE",)
    assert decision.demo_observation_allowed is False
    assert decision.live_promotion_allowed is False


def test_two_trade_three_day_smoke_stays_shadow_even_when_profitable() -> None:
    decision = evaluate_crypto_replay_evidence(
        _evidence(
            closed_trade_count=2,
            accepted_trade_plan_event_count=2,
            total_net_pnl_usdt=Decimal("15"),
            profit_factor=None,
            maximum_drawdown_pct=Decimal("0.87"),
            fees_usdt=Decimal("4.72"),
            observed_days=Decimal("3"),
        )
    )

    assert decision.posture is CryptoResearchPosture.HOLD_SHADOW_INSUFFICIENT_SAMPLE
    assert decision.reasons == (
        "CLOSED_TRADE_SAMPLE_TOO_SMALL",
        "OBSERVATION_WINDOW_TOO_SHORT",
    )
    assert decision.demo_observation_allowed is False


def test_sufficient_but_negative_economics_requires_retune() -> None:
    decision = evaluate_crypto_replay_evidence(
        _evidence(total_net_pnl_usdt=Decimal("-25"), profit_factor=Decimal("0.8"))
    )

    assert decision.posture is CryptoResearchPosture.RETUNE_NEGATIVE_ECONOMICS
    assert decision.demo_observation_allowed is False


def test_risk_or_fee_burden_blocks_demo_observation() -> None:
    decision = evaluate_crypto_replay_evidence(
        _evidence(
            maximum_drawdown_pct=Decimal("7"),
            fees_usdt=Decimal("150"),
            risk_budget_breach_count=1,
        )
    )

    assert decision.posture is CryptoResearchPosture.RETUNE_RISK_QUALITY
    assert set(decision.reasons) == {
        "MAXIMUM_DRAWDOWN_TOO_HIGH",
        "EXECUTION_COST_BURDEN_TOO_HIGH",
        "RISK_BUDGET_BREACHES_PRESENT",
    }
    assert decision.demo_observation_allowed is False


def test_historical_floor_can_only_unlock_demo_observation_not_live() -> None:
    policy = CryptoEvidencePolicy(
        minimum_closed_trades_for_demo_observation=30,
        minimum_observed_days_for_demo_observation=Decimal("14"),
    )

    decision = evaluate_crypto_replay_evidence(_evidence(), policy)

    assert decision.posture is CryptoResearchPosture.ELIGIBLE_FOR_DEMO_OBSERVATION
    assert decision.demo_observation_allowed is True
    assert decision.live_promotion_allowed is False
