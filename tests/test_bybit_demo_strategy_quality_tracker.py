from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorResult,
    BybitDemoOrchestratorStatus,
)
from app.execution.bybit_demo_strategy_quality_tracker import (
    summarize_bybit_demo_strategy_cycle_quality,
)
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleResult,
    BybitDemoStrategyCycleStatus,
    BybitDemoStrategySelection,
    BybitDemoStrategySelectionStatus,
)
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-18T00:35:00+00:00",
        reference_price=Decimal("108"),
        notional_usdt=Decimal("500"),
        reference_quantity=Decimal("4.6"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.018"),
        estimated_round_trip_cost_usdt=Decimal("0.8"),
        estimated_stop_loss_after_cost_usdt=Decimal("9.8"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0416"),
        expected_move_fraction=Decimal("0.06"),
        expected_net_edge_usd=Decimal("29.2"),
        quality_score=Decimal("2.1"),
    )


def _selection(
    status: BybitDemoStrategySelectionStatus,
    *,
    plan: CryptoTradePlan | None,
    executable_count: int = 0,
    economic_differs: bool = False,
    reasons: tuple[str, ...] = (),
) -> BybitDemoStrategySelection:
    return BybitDemoStrategySelection(
        status=status,
        reasons=reasons,
        selected_trade_plan=plan,
        selected_entry_preflight=None,
        selected_signal_rank=None if plan is None else 1,
        candidate_audit=(),
        executable_candidate_count=executable_count,
        economic_shadow_selected_symbol=None if plan is None else plan.symbol,
        economic_shadow_selected_side=None if plan is None else plan.side.value,
        economic_shadow_differs_from_current=economic_differs,
    )


def _orchestrator_result() -> BybitDemoOrchestratorResult:
    return BybitDemoOrchestratorResult(
        status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
        reasons=(),
        cycle_result=None,
        previous_trade_gate_checked=False,
        next_entry_allowed=False,
    )


def test_strategy_cycle_tracker_separates_pre_entry_funnel_from_realized_profit() -> None:
    plan = _plan()
    cycles = (
        BybitDemoStrategyCycleResult(
            status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
            selection=_selection(
                BybitDemoStrategySelectionStatus.SELECTED,
                plan=plan,
                executable_count=2,
                economic_differs=True,
            ),
            orchestrator_result=_orchestrator_result(),
            pre_entry_quote_checked=True,
            pre_entry_quote_price=Decimal("108.1"),
            pre_entry_modeled_entry_price=Decimal("108.12162"),
            pre_entry_quote_resized=True,
            pre_entry_original_quantity=Decimal("10"),
            pre_entry_adjusted_quantity=Decimal("8"),
        ),
        BybitDemoStrategyCycleResult(
            status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
            selection=_selection(
                BybitDemoStrategySelectionStatus.SELECTED,
                plan=plan,
                executable_count=1,
            ),
            orchestrator_result=None,
            pre_entry_quote_checked=True,
            pre_entry_quote_price=Decimal("10"),
            pre_entry_modeled_entry_price=Decimal("10.002"),
            pre_entry_quote_resized=False,
            pre_entry_original_quantity=Decimal("4.6"),
            pre_entry_adjusted_quantity=Decimal("4.6"),
            pre_entry_quote_reasons=("NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET",),
        ),
        BybitDemoStrategyCycleResult(
            status=BybitDemoStrategyCycleStatus.NO_TRADE,
            selection=_selection(
                BybitDemoStrategySelectionStatus.SESSION_RISK_BLOCKED,
                plan=None,
                reasons=("SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED",),
            ),
            orchestrator_result=None,
        ),
    )

    report = summarize_bybit_demo_strategy_cycle_quality(cycles)

    assert report["cycle_count"] == 3
    assert report["selected_plan_count"] == 2
    assert report["guarded_orchestrator_called_count"] == 1
    assert report["pre_entry_quote_checked_count"] == 2
    assert report["pre_entry_quote_blocked_count"] == 1
    assert report["pre_entry_quote_resized_count"] == 1
    assert report["session_risk_blocked_count"] == 1
    assert report["economic_shadow_comparable_decision_count"] == 1
    assert report["economic_shadow_disagreement_count"] == 1
    assert report["economic_shadow_disagreement_fraction"] == 1.0
    assert report["quote_quantity_retention"] == {
        "count": 2,
        "minimum": 0.8,
        "maximum": 1.0,
        "mean": 0.9,
    }
    assert report["selected_expected_net_edge_total_usd"] == 58.4
    assert report["pre_entry_quality_is_not_realized_profit"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["live_mainnet_order_routing_allowed"] is False


def test_strategy_cycle_tracker_rejects_quantity_increase() -> None:
    cycle = BybitDemoStrategyCycleResult(
        status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
        selection=_selection(
            BybitDemoStrategySelectionStatus.SELECTED,
            plan=_plan(),
            executable_count=1,
        ),
        orchestrator_result=None,
        pre_entry_quote_checked=True,
        pre_entry_original_quantity=Decimal("1"),
        pre_entry_adjusted_quantity=Decimal("1.1"),
    )
    with pytest.raises(ValueError, match="cannot increase quantity"):
        summarize_bybit_demo_strategy_cycle_quality((cycle,))


def test_strategy_cycle_tracker_rejects_orchestrator_result_on_blocked_cycle() -> None:
    cycle = BybitDemoStrategyCycleResult(
        status=BybitDemoStrategyCycleStatus.NO_TRADE,
        selection=_selection(BybitDemoStrategySelectionStatus.NO_EXECUTABLE_PLAN, plan=None),
        orchestrator_result=_orchestrator_result(),
    )
    with pytest.raises(ValueError, match="blocked/no-trade"):
        summarize_bybit_demo_strategy_cycle_quality((cycle,))
