from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleResult,
    BybitDemoStrategyCycleStatus,
    BybitDemoStrategySelectionStatus,
)

_ZERO = Decimal("0")


def summarize_bybit_demo_strategy_cycle_quality(
    cycles: Sequence[BybitDemoStrategyCycleResult],
) -> dict[str, Any]:
    """Summarize pre-entry/demo-orchestration quality without treating it as realized PnL."""

    cycle_status_counts: Counter[str] = Counter()
    selection_status_counts: Counter[str] = Counter()
    selection_reason_counts: Counter[str] = Counter()
    portfolio_reason_counts: Counter[str] = Counter()
    quote_block_reason_counts: Counter[str] = Counter()
    selected_symbol_counts: Counter[str] = Counter()
    selected_side_counts: Counter[str] = Counter()
    open_position_symbol_counts: Counter[str] = Counter()
    quote_checked_count = 0
    quote_resized_count = 0
    guarded_orchestrator_count = 0
    portfolio_state_checked_count = 0
    cycles_with_open_positions_count = 0
    correlation_block_count = 0
    economic_comparable_count = 0
    economic_disagreement_count = 0
    selected_plan_count = 0
    selected_expected_edge_total = _ZERO
    quantity_retention: list[Decimal] = []

    for cycle in cycles:
        if cycle.live_mainnet_order_routing_allowed:
            raise ValueError("demo strategy quality tracker rejected mainnet-capable cycle")
        if cycle.selection.live_mainnet_order_routing_allowed:
            raise ValueError("demo strategy quality tracker rejected mainnet-capable selection")
        if cycle.selection.order_write_performed:
            raise ValueError("demo strategy selection unexpectedly claims it wrote an order")

        cycle_status_counts[cycle.status.value] += 1
        selection_status_counts[cycle.selection.status.value] += 1
        selection_reason_counts.update(cycle.selection.reasons)
        quote_block_reason_counts.update(cycle.pre_entry_quote_reasons)
        correlation_block_count += cycle.selection.correlation_block_count
        if cycle.selection.portfolio_state_checked:
            portfolio_state_checked_count += 1
        if cycle.selection.open_position_symbols:
            cycles_with_open_positions_count += 1
            open_position_symbol_counts.update(cycle.selection.open_position_symbols)
        for audit in cycle.selection.candidate_audit:
            portfolio_reason_counts.update(audit.portfolio_reasons)

        if cycle.pre_entry_quote_checked:
            quote_checked_count += 1
        if cycle.pre_entry_quote_resized:
            quote_resized_count += 1
        if cycle.status is BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED:
            if cycle.orchestrator_result is None:
                raise ValueError("guarded-orchestrator cycle is missing its orchestrator result")
            guarded_orchestrator_count += 1
        elif cycle.orchestrator_result is not None:
            raise ValueError("blocked/no-trade cycle cannot carry an orchestrator result")

        if cycle.selection.executable_candidate_count >= 2:
            economic_comparable_count += 1
            if cycle.selection.economic_shadow_differs_from_current:
                economic_disagreement_count += 1
        elif cycle.selection.economic_shadow_differs_from_current:
            raise ValueError("economic shadow disagreement requires at least two executable plans")

        plan = cycle.selection.selected_trade_plan
        if plan is not None:
            selected_plan_count += 1
            selected_symbol_counts[plan.symbol] += 1
            selected_side_counts[plan.side.value] += 1
            selected_expected_edge_total += plan.expected_net_edge_usd

        original = cycle.pre_entry_original_quantity
        adjusted = cycle.pre_entry_adjusted_quantity
        if original is None and adjusted is None:
            continue
        if original is None or adjusted is None or original <= 0 or adjusted < 0:
            raise ValueError("demo strategy quote quantity audit is incomplete or invalid")
        if adjusted > original:
            raise ValueError("demo strategy quote guard cannot increase quantity")
        quantity_retention.append(adjusted / original)

    no_trade_count = cycle_status_counts[BybitDemoStrategyCycleStatus.NO_TRADE.value]
    portfolio_state_blocked_count = cycle_status_counts[
        BybitDemoStrategyCycleStatus.PORTFOLIO_STATE_BLOCKED.value
    ]
    quote_block_count = cycle_status_counts[
        BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED.value
    ]
    session_block_count = selection_status_counts[
        BybitDemoStrategySelectionStatus.SESSION_RISK_BLOCKED.value
    ]
    concurrency_block_count = selection_status_counts[
        BybitDemoStrategySelectionStatus.PORTFOLIO_CONCURRENCY_BLOCKED.value
    ]
    no_executable_plan_count = selection_status_counts[
        BybitDemoStrategySelectionStatus.NO_EXECUTABLE_PLAN.value
    ]
    cycle_count = len(cycles)
    return {
        "qualification": "BYBIT_DEMO_STRATEGY_CYCLE_QUALITY_TRACKER",
        "cycle_count": cycle_count,
        "selected_plan_count": selected_plan_count,
        "no_trade_count": no_trade_count,
        "session_risk_blocked_count": session_block_count,
        "portfolio_state_checked_count": portfolio_state_checked_count,
        "portfolio_state_blocked_count": portfolio_state_blocked_count,
        "portfolio_concurrency_blocked_count": concurrency_block_count,
        "cycles_with_open_positions_count": cycles_with_open_positions_count,
        "correlation_block_count": correlation_block_count,
        "no_executable_plan_count": no_executable_plan_count,
        "pre_entry_quote_checked_count": quote_checked_count,
        "pre_entry_quote_blocked_count": quote_block_count,
        "pre_entry_quote_resized_count": quote_resized_count,
        "guarded_orchestrator_called_count": guarded_orchestrator_count,
        "guarded_orchestrator_call_fraction": (
            None
            if cycle_count == 0
            else float(Decimal(guarded_orchestrator_count) / Decimal(cycle_count))
        ),
        "pre_entry_block_or_no_trade_fraction": (
            None
            if cycle_count == 0
            else float(Decimal(cycle_count - guarded_orchestrator_count) / Decimal(cycle_count))
        ),
        "economic_shadow_comparable_decision_count": economic_comparable_count,
        "economic_shadow_disagreement_count": economic_disagreement_count,
        "economic_shadow_disagreement_fraction": (
            None
            if economic_comparable_count == 0
            else float(Decimal(economic_disagreement_count) / Decimal(economic_comparable_count))
        ),
        "selected_expected_net_edge_total_usd": float(selected_expected_edge_total),
        "quote_quantity_retention": _distribution(quantity_retention),
        "cycle_status_counts": dict(sorted(cycle_status_counts.items())),
        "selection_status_counts": dict(sorted(selection_status_counts.items())),
        "selection_reason_counts": dict(sorted(selection_reason_counts.items())),
        "portfolio_candidate_reason_counts": dict(sorted(portfolio_reason_counts.items())),
        "pre_entry_quote_block_reason_counts": dict(sorted(quote_block_reason_counts.items())),
        "open_position_symbol_counts": dict(sorted(open_position_symbol_counts.items())),
        "selected_symbol_counts": dict(sorted(selected_symbol_counts.items())),
        "selected_side_counts": dict(sorted(selected_side_counts.items())),
        "economic_shadow_activation_allowed": False,
        "pre_entry_quality_is_not_realized_profit": True,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _distribution(values: Sequence[Decimal]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None}
    return {
        "count": len(values),
        "minimum": float(min(values)),
        "maximum": float(max(values)),
        "mean": float(sum(values, start=_ZERO) / Decimal(len(values))),
    }
