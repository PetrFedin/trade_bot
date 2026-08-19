from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPhase
from app.execution.bybit_demo_runtime_lease import BybitDemoRuntimeLease
from app.execution.bybit_demo_terminal_handoff import BybitDemoTerminalHandoffStatus
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeSafetyError,
    BybitDemoTradingRuntimeStatus,
    run_bybit_demo_trading_runtime,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


@dataclass(frozen=True)
class _State:
    symbol: str = "BTCUSDT"


@dataclass(frozen=True)
class _Checkpoint:
    state: _State = _State()


@dataclass(frozen=True)
class _SafeEntryResult:
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _ManagedPoll:
    phase: BybitDemoManagedTradePollPhase
    reasons: tuple[str, ...] = ()
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Handoff:
    status: BybitDemoTerminalHandoffStatus
    reasons: tuple[str, ...] = ()
    live_mainnet_order_routing_allowed: bool = False


class _SafeDependency:
    live_mainnet_order_routing_allowed = False


class _EvidenceStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, value: object = None, *, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if self.error is not None:
            raise self.error
        if self.value is None:
            raise FileNotFoundError
        return self.value


class _Lease:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_stale_takeover_allowed = False

    def __init__(self, *, busy: bool = False, acquire_error: Exception | None = None) -> None:
        self.busy = busy
        self.acquire_error = acquire_error
        self.release_error: Exception | None = None
        self.acquire_calls = 0
        self.release_calls = 0
        self.owner = "a" * 64

    def acquire(self) -> BybitDemoRuntimeLease:
        self.acquire_calls += 1
        if self.acquire_error is not None:
            raise self.acquire_error
        if self.busy:
            raise FileExistsError
        return BybitDemoRuntimeLease(
            owner_token=self.owner,
            created_time_ms=1,
            process_id=1,
        )

    def release(self, *, owner_token: str) -> None:
        self.release_calls += 1
        assert owner_token == self.owner
        if self.release_error is not None:
            raise self.release_error


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


def _session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
    )


def _runtime(**overrides: object):
    arguments: dict[str, object] = {
        "bars_by_symbol": {},
        "instruments": {"BTCUSDT": _instrument()},
        "strategy_config": CryptoPerpStrategyConfig(),
        "session_state": _session(),
        "now": datetime(2026, 8, 19, tzinfo=UTC),
        "now_ms": 1_000,
        "client": _SafeDependency(),
        "accounting_client": None,
        "excursion_store": _ExcursionStore(),
        "completed_bar_client": _SafeDependency(),
        "quote_client": _SafeDependency(),
        "runtime_lease": _Lease(),
    }
    arguments.update(overrides)
    return run_bybit_demo_trading_runtime(**arguments)


def test_busy_runtime_lease_blocks_before_checkpoint_or_entry() -> None:
    store = _ExcursionStore()
    lease = _Lease(busy=True)
    entry_calls = 0

    def _entry(*_args: object, **_kwargs: object) -> _SafeEntryResult:
        nonlocal entry_calls
        entry_calls += 1
        return _SafeEntryResult()

    result = _runtime(
        excursion_store=store,
        runtime_lease=lease,
        entry_executor=_entry,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.RUNTIME_BUSY
    assert store.load_calls == 0
    assert entry_calls == 0
    assert lease.release_calls == 0
    assert result.next_entry_allowed is False


def test_missing_checkpoint_executes_entry_once_under_lease_with_shared_quote() -> None:
    store = _ExcursionStore()
    lease = _Lease()
    quote = _SafeDependency()
    entry_calls = 0
    poll_calls = 0
    seen_quote = None

    def _entry(*_args: object, **kwargs: object) -> _SafeEntryResult:
        nonlocal entry_calls, seen_quote
        entry_calls += 1
        seen_quote = kwargs["quote_client"]
        assert kwargs["excursion_store"] is store
        return _SafeEntryResult()

    def _poll(**_kwargs: object) -> _ManagedPoll:
        nonlocal poll_calls
        poll_calls += 1
        return _ManagedPoll(BybitDemoManagedTradePollPhase.OPEN_MANAGED)

    result = _runtime(
        excursion_store=store,
        runtime_lease=lease,
        quote_client=quote,
        entry_executor=_entry,
        managed_poller=_poll,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED
    assert entry_calls == 1
    assert poll_calls == 0
    assert seen_quote is quote
    assert lease.release_calls == 1
    assert result.runtime_lease_released is True
    assert result.same_invocation_additional_entry_allowed is False


def test_active_checkpoint_routes_only_to_management_never_new_selection() -> None:
    store = _ExcursionStore(_Checkpoint())
    entry_calls = 0
    poll_calls = 0

    def _entry(*_args: object, **_kwargs: object) -> _SafeEntryResult:
        nonlocal entry_calls
        entry_calls += 1
        return _SafeEntryResult()

    def _poll(**kwargs: object) -> _ManagedPoll:
        nonlocal poll_calls
        poll_calls += 1
        assert kwargs["instrument"].symbol == "BTCUSDT"
        return _ManagedPoll(
            BybitDemoManagedTradePollPhase.OPEN_MANAGED,
            reasons=("OPEN_POSITION_MANAGED",),
        )

    result = _runtime(
        excursion_store=store,
        entry_executor=_entry,
        managed_poller=_poll,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED
    assert result.reasons == ("OPEN_POSITION_MANAGED",)
    assert entry_calls == 0
    assert poll_calls == 1
    assert result.next_entry_allowed is False


def test_corrupt_checkpoint_blocks_without_falling_back_to_entry() -> None:
    store = _ExcursionStore(error=ValueError("bad checksum"))
    entry_calls = 0
    poll_calls = 0

    def _entry(*_args: object, **_kwargs: object) -> _SafeEntryResult:
        nonlocal entry_calls
        entry_calls += 1
        return _SafeEntryResult()

    def _poll(**_kwargs: object) -> _ManagedPoll:
        nonlocal poll_calls
        poll_calls += 1
        return _ManagedPoll(BybitDemoManagedTradePollPhase.OPEN_MANAGED)

    result = _runtime(
        excursion_store=store,
        entry_executor=_entry,
        managed_poller=_poll,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED
    assert result.reasons == ("ACTIVE_DEMO_EXCURSION_LOAD_FAILED:ValueError",)
    assert entry_calls == 0
    assert poll_calls == 0
    assert result.runtime_lease_released is True


def test_active_checkpoint_with_missing_instrument_blocks_management_and_entry() -> None:
    result = _runtime(
        excursion_store=_ExcursionStore(_Checkpoint()),
        instruments={},
        entry_executor=lambda *_args, **_kwargs: _SafeEntryResult(),
        managed_poller=lambda **_kwargs: _ManagedPoll(
            BybitDemoManagedTradePollPhase.OPEN_MANAGED
        ),
    )

    assert result.status is BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED
    assert result.reasons == ("ACTIVE_DEMO_EXCURSION_INSTRUMENT_MISSING",)


def test_terminal_evidence_ready_without_store_never_clears_or_reenters() -> None:
    handoff_calls = 0

    def _handoff(*_args: object, **_kwargs: object) -> _Handoff:
        nonlocal handoff_calls
        handoff_calls += 1
        return _Handoff(BybitDemoTerminalHandoffStatus.COMPLETE)

    result = _runtime(
        excursion_store=_ExcursionStore(_Checkpoint()),
        managed_poller=lambda **_kwargs: _ManagedPoll(
            BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY
        ),
        terminal_handoff=_handoff,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED
    assert result.reasons == ("TERMINAL_EVIDENCE_STORE_REQUIRED_FOR_FINAL_HANDOFF",)
    assert handoff_calls == 0
    assert result.next_entry_allowed is False


def test_terminal_handoff_complete_allows_only_next_invocation_not_same_call_entry() -> None:
    entry_calls = 0
    handoff_calls = 0

    def _entry(*_args: object, **_kwargs: object) -> _SafeEntryResult:
        nonlocal entry_calls
        entry_calls += 1
        return _SafeEntryResult()

    def _handoff(*_args: object, **_kwargs: object) -> _Handoff:
        nonlocal handoff_calls
        handoff_calls += 1
        return _Handoff(BybitDemoTerminalHandoffStatus.COMPLETE)

    result = _runtime(
        excursion_store=_ExcursionStore(_Checkpoint()),
        terminal_evidence_store=_EvidenceStore(),
        entry_executor=_entry,
        managed_poller=lambda **_kwargs: _ManagedPoll(
            BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY
        ),
        terminal_handoff=_handoff,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE
    assert handoff_calls == 1
    assert entry_calls == 0
    assert result.next_entry_allowed is True
    assert result.same_invocation_additional_entry_allowed is False
    assert result.runtime_lease_released is True


def test_lease_release_failure_blocks_future_entry_even_after_operation() -> None:
    lease = _Lease()
    lease.release_error = RuntimeError("fsync failed")

    result = _runtime(
        runtime_lease=lease,
        entry_executor=lambda *_args, **_kwargs: _SafeEntryResult(),
    )

    assert result.status is BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED
    assert result.reasons == ("DEMO_RUNTIME_LEASE_RELEASE_FAILED:RuntimeError",)
    assert result.entry_result is not None
    assert result.runtime_lease_released is False
    assert result.next_entry_allowed is False


def test_unsafe_entry_result_is_hard_rejected_after_lease_release() -> None:
    unsafe = _SafeEntryResult(live_mainnet_order_routing_allowed=True)
    lease = _Lease()

    with pytest.raises(
        BybitDemoTradingRuntimeSafetyError,
        match="mainnet-capable entry cycle",
    ):
        _runtime(
            runtime_lease=lease,
            entry_executor=lambda *_args, **_kwargs: unsafe,
        )

    assert lease.release_calls == 1
