from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo_cycle import BybitDemoCycleStatus
from app.execution.bybit_demo_protection_reconciliation import (
    BybitDemoProtectionReconciledOrchestratorResult,
)


def summarize_bybit_demo_protection_quality(
    results: Sequence[BybitDemoProtectionReconciledOrchestratorResult],
) -> dict[str, Any]:
    """Aggregate exchange-protection proof quality without treating it as realized PnL."""

    reason_counts: Counter[str] = Counter()
    flatten_reason_counts: Counter[str] = Counter()
    exit_mode_counts: Counter[str] = Counter()
    checked_count = 0
    reconciled_count = 0
    retry_count = 0
    flatten_requested_count = 0
    flatten_failed_count = 0
    flatten_confirmed_closed_count = 0
    flatten_unconfirmed_count = 0
    flatten_read_failed_count = 0
    flatten_retry_count = 0
    flatten_total_attempts = 0
    flatten_max_attempts = 0
    flatten_max_residual_size = Decimal("0")
    post_protection_liquidation_block_count = 0
    runner_checked_count = 0
    runner_active_price_unobservable_count = 0
    total_attempts = 0
    max_attempts = 0

    for result in results:
        if result.live_mainnet_order_routing_allowed:
            raise ValueError("protection quality tracker rejected mainnet-capable result")
        if result.protection_state_reconciled and not result.protection_state_checked:
            raise ValueError("reconciled protection result must be marked checked")
        if result.protection_reconciliation_attempts < 0:
            raise ValueError("protection reconciliation attempts cannot be negative")
        if not result.protection_state_checked and result.protection_reconciliation_attempts != 0:
            raise ValueError("unchecked protection result cannot report reconciliation attempts")
        _validate_flatten_telemetry(result)

        cycle = result.cycle_result
        if cycle is not None and cycle.exit_mode is not None:
            exit_mode_counts[cycle.exit_mode] += 1

        if result.emergency_flatten_requested:
            flatten_requested_count += 1
            flatten_total_attempts += result.emergency_flatten_reconciliation_attempts
            flatten_max_attempts = max(
                flatten_max_attempts,
                result.emergency_flatten_reconciliation_attempts,
            )
            if result.emergency_flatten_reconciliation_attempts > 1:
                flatten_retry_count += 1
            flatten_reason = result.emergency_flatten_reconciliation_reason
            if flatten_reason is None:
                raise ValueError("requested emergency flatten is missing reconciliation reason")
            flatten_reason_counts[flatten_reason] += 1
            if result.emergency_flatten_position_closed:
                flatten_confirmed_closed_count += 1
            else:
                flatten_unconfirmed_count += 1
                if flatten_reason.startswith("EMERGENCY_FLATTEN_POSITION_READ_FAILED:"):
                    flatten_read_failed_count += 1
                residual = result.emergency_flatten_residual_size
                if residual is not None:
                    flatten_max_residual_size = max(flatten_max_residual_size, residual)

        if not result.protection_state_checked:
            continue
        checked_count += 1
        total_attempts += result.protection_reconciliation_attempts
        max_attempts = max(max_attempts, result.protection_reconciliation_attempts)
        if result.protection_reconciliation_attempts > 1:
            retry_count += 1
        if result.protection_state_reason is None:
            raise ValueError("checked protection result is missing a reason")
        reason_counts[result.protection_state_reason] += 1
        if result.protection_state_reconciled:
            reconciled_count += 1

        if cycle is None:
            raise ValueError("checked protection result is missing cycle evidence")
        if cycle.exit_mode == "OPEN_ENDED_RUNNER":
            runner_checked_count += 1
            if not result.runner_active_price_observable:
                runner_active_price_unobservable_count += 1

        if cycle.status is BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED:
            flatten_failed_count += 1
        if any(
            reason.startswith("POST_PROTECTION_LIQUIDATION_")
            for reason in cycle.reasons
        ):
            post_protection_liquidation_block_count += 1

    unverified_count = checked_count - reconciled_count
    return {
        "qualification": "BYBIT_DEMO_EXCHANGE_PROTECTION_QUALITY_TRACKER",
        "result_count": len(results),
        "protection_state_checked_count": checked_count,
        "protection_state_reconciled_count": reconciled_count,
        "protection_state_unverified_count": unverified_count,
        "protection_state_reconciled_fraction": (
            None
            if checked_count == 0
            else float(Decimal(reconciled_count) / Decimal(checked_count))
        ),
        "protection_reconciliation_retry_count": retry_count,
        "protection_reconciliation_mean_attempts": (
            None
            if checked_count == 0
            else float(Decimal(total_attempts) / Decimal(checked_count))
        ),
        "protection_reconciliation_max_attempts": max_attempts,
        "protection_fail_closed_flatten_requested_count": flatten_requested_count,
        "protection_fail_closed_flatten_failed_count": flatten_failed_count,
        "emergency_flatten_confirmed_closed_count": flatten_confirmed_closed_count,
        "emergency_flatten_unconfirmed_count": flatten_unconfirmed_count,
        "emergency_flatten_position_read_failed_count": flatten_read_failed_count,
        "emergency_flatten_reconciliation_retry_count": flatten_retry_count,
        "emergency_flatten_reconciliation_mean_attempts": (
            None
            if flatten_requested_count == 0
            else float(Decimal(flatten_total_attempts) / Decimal(flatten_requested_count))
        ),
        "emergency_flatten_reconciliation_max_attempts": flatten_max_attempts,
        "emergency_flatten_max_residual_size": float(flatten_max_residual_size),
        "post_protection_liquidation_block_count": post_protection_liquidation_block_count,
        "runner_protection_checked_count": runner_checked_count,
        "runner_active_price_unobservable_count": runner_active_price_unobservable_count,
        "protection_state_reason_counts": dict(sorted(reason_counts.items())),
        "emergency_flatten_reconciliation_reason_counts": dict(
            sorted(flatten_reason_counts.items())
        ),
        "exit_mode_counts": dict(sorted(exit_mode_counts.items())),
        "reduce_only_order_ack_is_not_close_confirmation": True,
        "runner_active_price_is_not_inferred_from_position_info": True,
        "protection_quality_is_not_realized_profit": True,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _validate_flatten_telemetry(
    result: BybitDemoProtectionReconciledOrchestratorResult,
) -> None:
    if result.emergency_flatten_reconciliation_attempts < 0:
        raise ValueError("emergency flatten reconciliation attempts cannot be negative")
    residual = result.emergency_flatten_residual_size
    if residual is not None and (not residual.is_finite() or residual < 0):
        raise ValueError("emergency flatten residual size must be finite and non-negative")
    if not result.emergency_flatten_requested:
        if (
            result.emergency_flatten_position_closed is not None
            or result.emergency_flatten_reconciliation_attempts != 0
            or residual is not None
            or result.emergency_flatten_reconciliation_reason is not None
        ):
            raise ValueError("unrequested emergency flatten cannot carry reconciliation evidence")
        return
    if result.emergency_flatten_position_closed is None:
        raise ValueError("requested emergency flatten must report position close state")
    if result.emergency_flatten_reconciliation_attempts < 1:
        raise ValueError("requested emergency flatten must report reconciliation attempts")
    if result.emergency_flatten_reconciliation_reason is None:
        raise ValueError("requested emergency flatten must report reconciliation reason")
    if result.emergency_flatten_position_closed and residual != Decimal("0"):
        raise ValueError("confirmed emergency flatten must have zero residual size")
