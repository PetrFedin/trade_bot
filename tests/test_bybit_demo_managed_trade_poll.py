from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    BybitDemoExcursionRuntimeStatus,
)
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    BybitDemoManagedTradePollPolicy,
    poll_bybit_demo_managed_trade,
)
from app.execution.bybit_demo_max_hold_close import (
    BybitDemoMaxHoldCloseResult,
    BybitDemoMaxHoldCloseStatus,
)
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimeResult,
    BybitDemoTradeManagementRuntimeStatus,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig


@dataclass(frozen=True)
class _SafeAccounting:
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _SafeEvidence:
    fully_reconciled_all_in: bool
    live_mainnet_order_routing_allowed: bool = False


class _SafeStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("1000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _excursion(
    status: BybitDemoExcursionRuntimeStatus,
    *,
    reasons: tuple[str, ...] = (),
    terminal_complete: bool = False,
) -> BybitDemoExcursionRuntimeResult:
    marker = object() if terminal_complete else None
    return BybitDemoExcursionRuntimeResult(
        status=status,
        reasons=reasons,
        checkpoint=marker,
        trade=marker,
        final=marker,
        checkpoint_clear_allowed=terminal_complete,
    )


def _management(
    status: BybitDemoTradeManagementRuntimeStatus,
    *,
    reasons: tuple[str, ...] = (),
) -> BybitDemoTradeManagementRuntimeResult:
    return BybitDemoTradeManagementRuntimeResult(
        status=status,
        reasons=reasons,
        decision=None,
        entry_execution_time_ms=None,
        entry_bucket_start_ms=None,
        protection_bar_start_ms=None,
        actual_entry_fee_usdt=None,
        fresh_last_price=None,
        ratchet_ack=None,
        post_write_position=None,
        stop_ratchet_write_attempted=False,
        stop_ratchet_verified=False,
    )


def _max_hold(status: BybitDemoMaxHoldCloseStatus) -> BybitDemoMaxHoldCloseResult:
    return BybitDemoMaxHoldCloseResult(
        status=status,
        reasons=(status.value,),
        close_request=None,
        close_ack=None,
        reconciliation_attempts=0,
        residual_size=None,
        position_closed=(status is BybitDemoMaxHoldCloseStatus.CLOSE_CONFIRMED),
    )


def _poll(**overrides: object):
    arguments: dict[str, object] = {
        "excursion_store": _SafeStore(),
        "trade_client": object(),
        "completed_bar_client": object(),
        "quote_client": object(),
        "instrument": _instrument(),
        "strategy_config": CryptoPerpStrategyConfig(),
        "now_ms": 1_000,
    }
    arguments.update(overrides)
    return poll_bybit_demo_managed_trade(**arguments)


def test_tracking_block_stops_before_trade_management() -> None:
    called = False

    def _manage(**_: object) -> BybitDemoTradeManagementRuntimeResult:
        nonlocal called
        called = True
        return _management(BybitDemoTradeManagementRuntimeStatus.NO_CHANGE)

    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=("EXECUTION_AMBIGUOUS",),
        ),
        run_management=_manage,
    )

    assert result.phase is BybitDemoManagedTradePollPhase.TRACKING_BLOCKED
    assert result.reasons == ("EXECUTION_AMBIGUOUS",)
    assert called is False
    assert result.next_entry_allowed is False


def test_open_trade_runs_management_after_excursion_observation() -> None:
    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED
        ),
        run_management=lambda **_: _management(
            BybitDemoTradeManagementRuntimeStatus.RATCHET_VERIFIED,
            reasons=("BASELINE_STOP_RATCHET_VERIFIED",),
        ),
    )

    assert result.phase is BybitDemoManagedTradePollPhase.OPEN_MANAGED
    assert result.management is not None
    assert result.management.status is BybitDemoTradeManagementRuntimeStatus.RATCHET_VERIFIED
    assert result.next_entry_allowed is False


def test_unverified_ratchet_blocks_managed_poll_without_claiming_trade_closed() -> None:
    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED
        ),
        run_management=lambda **_: _management(
            BybitDemoTradeManagementRuntimeStatus.RATCHET_UNVERIFIED,
            reasons=("EXCHANGE_STOP_STATE_MISMATCH",),
        ),
    )

    assert result.phase is BybitDemoManagedTradePollPhase.TRACKING_BLOCKED
    assert result.reasons == ("EXCHANGE_STOP_STATE_MISMATCH",)
    assert result.max_hold_close is None
    assert result.next_entry_allowed is False


def test_max_hold_uses_separate_executor_and_ack_does_not_enable_reentry() -> None:
    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED
        ),
        run_management=lambda **_: _management(
            BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED,
            reasons=("BASELINE_MAXIMUM_HOLDING_BARS_REACHED",),
        ),
        run_max_hold_close=lambda *_args, **_kwargs: _max_hold(
            BybitDemoMaxHoldCloseStatus.CLOSE_CONFIRMED
        ),
    )

    assert result.phase is BybitDemoManagedTradePollPhase.MAX_HOLD_ACTION
    assert result.max_hold_close is not None
    assert result.max_hold_close.position_closed is True
    assert result.next_entry_allowed is False
    assert result.terminal_evidence_ack_required is False


def test_max_hold_writes_remain_disabled_by_default() -> None:
    seen_policy = None

    def _close(*_args: object, **kwargs: object) -> BybitDemoMaxHoldCloseResult:
        nonlocal seen_policy
        seen_policy = kwargs["policy"]
        return _max_hold(BybitDemoMaxHoldCloseStatus.WRITES_DISABLED)

    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED
        ),
        run_management=lambda **_: _management(
            BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED
        ),
        run_max_hold_close=_close,
    )

    assert result.phase is BybitDemoManagedTradePollPhase.MAX_HOLD_ACTION
    assert isinstance(seen_policy, object)
    assert seen_policy.writes_enabled is False


def test_terminal_trade_never_runs_management_and_requires_accounting() -> None:
    management_called = False

    def _manage(**_: object) -> BybitDemoTradeManagementRuntimeResult:
        nonlocal management_called
        management_called = True
        return _management(BybitDemoTradeManagementRuntimeStatus.NO_CHANGE)

    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
            terminal_complete=True,
        ),
        run_management=_manage,
    )

    assert result.phase is BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING
    assert result.reasons == ("TERMINAL_ACCOUNTING_CLIENT_REQUIRED",)
    assert result.terminal_evidence_ack_required is True
    assert management_called is False
    assert result.next_entry_allowed is False


def test_fully_reconciled_terminal_evidence_still_requires_durable_ack() -> None:
    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
            terminal_complete=True,
        ),
        accounting_client=object(),
        run_accounting=lambda *_args, **_kwargs: _SafeAccounting(),
        build_profit_evidence=lambda *_args, **_kwargs: _SafeEvidence(True),
    )

    assert result.phase is BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY
    assert result.fully_reconciled_all_in is True
    assert result.terminal_evidence_ack_required is True
    assert result.next_entry_allowed is False
    assert result.profit_evidence is not None


def test_pending_all_in_accounting_does_not_classify_terminal_evidence_ready() -> None:
    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
            terminal_complete=True,
        ),
        accounting_client=object(),
        run_accounting=lambda *_args, **_kwargs: _SafeAccounting(),
        build_profit_evidence=lambda *_args, **_kwargs: _SafeEvidence(False),
    )

    assert result.phase is BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING
    assert result.reasons == ("TERMINAL_ALL_IN_ACCOUNTING_PENDING",)
    assert result.fully_reconciled_all_in is False
    assert result.terminal_evidence_ack_required is True


def test_terminal_accounting_failure_preserves_checkpoint_for_retry() -> None:
    def _fail(*_args: object, **_kwargs: object) -> _SafeAccounting:
        raise RuntimeError("accounting unavailable")

    result = _poll(
        advance_excursion=lambda **_: _excursion(
            BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
            terminal_complete=True,
        ),
        accounting_client=object(),
        run_accounting=_fail,
    )

    assert result.phase is BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING
    assert result.reasons == ("TERMINAL_ACCOUNTING_OR_EVIDENCE_FAILED:RuntimeError",)
    assert result.terminal_evidence_ack_required is True
    assert result.next_entry_allowed is False


def test_mainnet_capable_dependency_result_is_rejected() -> None:
    unsafe = _excursion(BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED)
    object.__setattr__(unsafe, "live_mainnet_order_routing_allowed", True)

    with pytest.raises(ValueError, match="mainnet-capable excursion"):
        _poll(advance_excursion=lambda **_: unsafe)


def test_policy_keeps_stop_and_max_hold_writes_disabled_by_default() -> None:
    policy = BybitDemoManagedTradePollPolicy()

    assert policy.trade_management.stop_ratchet_writes_enabled is False
    assert policy.max_hold_close.writes_enabled is False
