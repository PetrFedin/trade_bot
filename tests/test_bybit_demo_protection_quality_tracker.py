from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo_cycle import BybitDemoCycleStatus
from app.execution.bybit_demo_protection_quality_tracker import (
    summarize_bybit_demo_protection_quality,
)


def _result(
    *,
    checked: bool = True,
    reconciled: bool = True,
    reason: str | None = "VERIFIED",
    attempts: int = 1,
    status: BybitDemoCycleStatus = BybitDemoCycleStatus.PROTECTED,
    exit_mode: str = "FIXED_20_TARGET",
    cycle_reasons: tuple[str, ...] = (),
    runner_active_price_observable: bool = False,
    flatten_requested: bool | None = None,
    flatten_closed: bool | None = None,
    flatten_attempts: int | None = None,
    flatten_residual_size: Decimal | None = None,
    flatten_reason: str | None = None,
):
    if flatten_requested is None:
        flatten_requested = status is BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED
    if flatten_requested:
        if flatten_closed is None:
            flatten_closed = True
        if flatten_attempts is None:
            flatten_attempts = 1
        if flatten_reason is None:
            flatten_reason = (
                "EMERGENCY_FLATTEN_CONFIRMED_CLOSED"
                if flatten_closed
                else "EMERGENCY_FLATTEN_RESIDUAL_POSITION"
            )
        if flatten_residual_size is None and flatten_closed:
            flatten_residual_size = Decimal("0")
    else:
        flatten_closed = None
        flatten_attempts = 0
        flatten_residual_size = None
        flatten_reason = None

    cycle = SimpleNamespace(
        status=status,
        exit_mode=exit_mode,
        reasons=cycle_reasons,
    )
    return SimpleNamespace(
        live_mainnet_order_routing_allowed=False,
        protection_state_checked=checked,
        protection_state_reconciled=reconciled,
        protection_state_reason=reason,
        protection_reconciliation_attempts=attempts,
        runner_active_price_observable=runner_active_price_observable,
        emergency_flatten_requested=flatten_requested,
        emergency_flatten_position_closed=flatten_closed,
        emergency_flatten_reconciliation_attempts=flatten_attempts,
        emergency_flatten_residual_size=flatten_residual_size,
        emergency_flatten_reconciliation_reason=flatten_reason,
        cycle_result=cycle,
    )


def test_protection_quality_separates_verified_retries_and_fail_closed_flatten() -> None:
    quality = summarize_bybit_demo_protection_quality(
        [
            _result(),
            _result(
                attempts=2,
                exit_mode="OPEN_ENDED_RUNNER",
            ),
            _result(
                reconciled=False,
                reason="STOP_LOSS_MISMATCH",
                status=BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
            ),
        ]
    )

    assert quality["protection_state_checked_count"] == 3
    assert quality["protection_state_reconciled_count"] == 2
    assert quality["protection_state_unverified_count"] == 1
    assert quality["protection_reconciliation_retry_count"] == 1
    assert quality["protection_reconciliation_max_attempts"] == 2
    assert quality["protection_fail_closed_flatten_requested_count"] == 1
    assert quality["emergency_flatten_confirmed_closed_count"] == 1
    assert quality["emergency_flatten_unconfirmed_count"] == 0
    assert quality["runner_protection_checked_count"] == 1
    assert quality["runner_active_price_unobservable_count"] == 1
    assert quality["protection_state_reason_counts"] == {
        "STOP_LOSS_MISMATCH": 1,
        "VERIFIED": 2,
    }
    assert quality["reduce_only_order_ack_is_not_close_confirmation"] is True
    assert quality["protection_quality_is_not_realized_profit"] is True
    assert quality["strategy_promotion_allowed"] is False


def test_protection_quality_tracks_fresh_liquidation_deterioration() -> None:
    quality = summarize_bybit_demo_protection_quality(
        [
            _result(
                reconciled=True,
                reason="VERIFIED",
                status=BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
                cycle_reasons=("POST_PROTECTION_LIQUIDATION_NOT_BEYOND_HARD_STOP",),
            )
        ]
    )

    assert quality["post_protection_liquidation_block_count"] == 1
    assert quality["protection_fail_closed_flatten_requested_count"] == 1


def test_protection_quality_tracks_residual_and_unreadable_flatten_state() -> None:
    quality = summarize_bybit_demo_protection_quality(
        [
            _result(
                reconciled=False,
                reason="STOP_LOSS_MISMATCH",
                status=BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
                flatten_closed=False,
                flatten_attempts=3,
                flatten_residual_size=Decimal("0.002"),
                flatten_reason="EMERGENCY_FLATTEN_RESIDUAL_POSITION",
            ),
            _result(
                reconciled=False,
                reason="PROTECTION_STATE_READ_FAILED:TimeoutError",
                status=BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
                flatten_closed=False,
                flatten_attempts=4,
                flatten_residual_size=None,
                flatten_reason="EMERGENCY_FLATTEN_POSITION_READ_FAILED:TimeoutError",
            ),
        ]
    )

    assert quality["emergency_flatten_confirmed_closed_count"] == 0
    assert quality["emergency_flatten_unconfirmed_count"] == 2
    assert quality["emergency_flatten_position_read_failed_count"] == 1
    assert quality["emergency_flatten_reconciliation_retry_count"] == 2
    assert quality["emergency_flatten_reconciliation_max_attempts"] == 4
    assert quality["emergency_flatten_max_residual_size"] == 0.002
    assert quality["emergency_flatten_reconciliation_reason_counts"] == {
        "EMERGENCY_FLATTEN_POSITION_READ_FAILED:TimeoutError": 1,
        "EMERGENCY_FLATTEN_RESIDUAL_POSITION": 1,
    }


def test_protection_quality_tracks_emergency_close_request_failure() -> None:
    quality = summarize_bybit_demo_protection_quality(
        [
            _result(
                reconciled=False,
                reason="PROTECTION_STATE_READ_FAILED:TimeoutError",
                status=BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED,
            )
        ]
    )

    assert quality["protection_fail_closed_flatten_failed_count"] == 1
    assert quality["protection_fail_closed_flatten_requested_count"] == 0


def test_protection_quality_rejects_inconsistent_checked_flags() -> None:
    with pytest.raises(ValueError, match="must be marked checked"):
        summarize_bybit_demo_protection_quality(
            [_result(checked=False, reconciled=True, reason=None, attempts=0)]
        )

    with pytest.raises(ValueError, match="cannot report reconciliation attempts"):
        summarize_bybit_demo_protection_quality(
            [_result(checked=False, reconciled=False, reason=None, attempts=1)]
        )


def test_protection_quality_rejects_inconsistent_flatten_telemetry() -> None:
    bad = _result()
    bad.emergency_flatten_requested = False
    bad.emergency_flatten_position_closed = True

    with pytest.raises(ValueError, match="unrequested emergency flatten"):
        summarize_bybit_demo_protection_quality([bad])


def test_protection_quality_accepts_non_protected_cycles_as_unchecked() -> None:
    quality = summarize_bybit_demo_protection_quality(
        [
            _result(
                checked=False,
                reconciled=False,
                reason=None,
                attempts=0,
                status=BybitDemoCycleStatus.ENTRY_BLOCKED,
            )
        ]
    )

    assert quality["result_count"] == 1
    assert quality["protection_state_checked_count"] == 0
    assert quality["protection_state_reconciled_fraction"] is None
