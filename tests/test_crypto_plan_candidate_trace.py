from decimal import Decimal

import pytest

from app.strategy.crypto_perp import CryptoSide, CryptoSignal, CryptoTradePlan
from app.strategy.crypto_plan_candidate_trace import (
    CryptoPlanEligibleCandidateTrace,
    build_crypto_plan_eligible_candidate_trace,
)


def _signal() -> CryptoSignal:
    return CryptoSignal(
        symbol="SOLUSDT",
        side=CryptoSide.LONG,
        reference_price=Decimal("100"),
        momentum=Decimal("0.02"),
        atr_fraction=Decimal("0.01"),
        fast_ema=Decimal("101"),
        slow_ema=Decimal("99"),
        breakout_strength_atr=Decimal("0.4"),
        one_bar_atr_multiple=Decimal("0.8"),
        average_turnover_usdt=Decimal("1000000"),
        quality_score=Decimal("4.2"),
        decision_time="2026-08-19T20:40:00+00:00",
    )


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="SOLUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-19T20:40:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("10"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal("1.60"),
        estimated_stop_loss_after_cost_usdt=Decimal("11.60"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0216"),
        expected_move_fraction=Decimal("0.04"),
        expected_net_edge_usd=Decimal("38.40"),
        quality_score=Decimal("4.2"),
    )


def test_plan_candidate_trace_uses_exact_economic_rank_inputs() -> None:
    trace = build_crypto_plan_eligible_candidate_trace(
        _signal(),
        _plan(),
        planned_execution_time="2026-08-19T20:45:00+00:00",
        open_position_count=1,
        already_pending_count=0,
        maximum_concurrent_positions=2,
    )
    payload = trace.to_payload()

    assert payload["event"] == "PLAN_ELIGIBLE"
    assert payload["symbol"] == "SOLUSDT"
    assert payload["expected_net_r"] == pytest.approx(3.84)
    assert payload["cost_to_target_fraction"] == pytest.approx(0.08)
    assert payload["available_slots_before_selection"] == 1
    assert payload["future_outcome_fields_present"] is False
    assert payload["selection_mutation_allowed"] is False
    assert payload["execution_mutation_allowed"] is False
    assert len(payload["trace_id"]) == 64


def test_plan_candidate_trace_is_stable_and_contains_no_outcome_fields() -> None:
    first = build_crypto_plan_eligible_candidate_trace(
        _signal(),
        _plan(),
        planned_execution_time="2026-08-19T20:45:00+00:00",
        open_position_count=2,
        already_pending_count=1,
        maximum_concurrent_positions=2,
    )
    second = build_crypto_plan_eligible_candidate_trace(
        _signal(),
        _plan(),
        planned_execution_time="2026-08-19T20:45:00+00:00",
        open_position_count=2,
        already_pending_count=1,
        maximum_concurrent_positions=2,
    )

    assert first.trace_id == second.trace_id
    assert first.available_slots_before_selection == 0
    forbidden_fragments = ("exit", "pnl", "mfe", "mae", "winner", "outcome")
    payload_keys = tuple(first.to_payload())
    assert not any(
        fragment in key.lower()
        for key in payload_keys
        for fragment in forbidden_fragments
        if key != "future_outcome_fields_present"
    )


def test_plan_candidate_trace_fails_closed_on_inconsistent_rank_math() -> None:
    trace = CryptoPlanEligibleCandidateTrace(
        decision_time="2026-08-19T20:40:00+00:00",
        planned_execution_time="2026-08-19T20:45:00+00:00",
        symbol="SOLUSDT",
        side="LONG",
        quality_score=Decimal("4.2"),
        expected_net_edge_usd=Decimal("38.4"),
        risk_budget_usdt=Decimal("10"),
        expected_net_r=Decimal("3.00"),
        estimated_round_trip_cost_usdt=Decimal("1.6"),
        target_net_profit_usd=Decimal("20"),
        cost_to_target_fraction=Decimal("0.08"),
        open_position_count=1,
        already_pending_count=0,
        maximum_concurrent_positions=2,
    )

    with pytest.raises(ValueError, match="expected-net-R"):
        trace.validate()
