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
):
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
    assert quality["runner_protection_checked_count"] == 1
    assert quality["runner_active_price_unobservable_count"] == 1
    assert quality["protection_state_reason_counts"] == {
        "STOP_LOSS_MISMATCH": 1,
        "VERIFIED": 2,
    }
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


def test_protection_quality_tracks_emergency_close_failure() -> None:
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


def test_protection_quality_rejects_inconsistent_checked_flags() -> None:
    with pytest.raises(ValueError, match="must be marked checked"):
        summarize_bybit_demo_protection_quality(
            [_result(checked=False, reconciled=True, reason=None, attempts=0)]
        )

    with pytest.raises(ValueError, match="cannot report reconciliation attempts"):
        summarize_bybit_demo_protection_quality(
            [_result(checked=False, reconciled=False, reason=None, attempts=1)]
        )


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
