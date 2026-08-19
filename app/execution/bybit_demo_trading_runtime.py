from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    BybitDemoManagedTradePollPolicy,
    BybitDemoManagedTradePollResult,
    poll_bybit_demo_managed_trade,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoResilientAccountSizedCycleResult,
    execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_runtime_lease import BybitDemoRuntimeLease
from app.execution.bybit_demo_terminal_handoff import (
    BybitDemoTerminalHandoffResult,
    BybitDemoTerminalHandoffStatus,
    persist_and_acknowledge_bybit_demo_terminal_evidence,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


class BybitDemoTradingRuntimeStatus(StrEnum):
    RUNTIME_BUSY = "RUNTIME_BUSY"
    RUNTIME_BLOCKED = "RUNTIME_BLOCKED"
    ENTRY_CYCLE_EXECUTED = "ENTRY_CYCLE_EXECUTED"
    ACTIVE_TRADE_POLLED = "ACTIVE_TRADE_POLLED"
    TERMINAL_HANDOFF_COMPLETE = "TERMINAL_HANDOFF_COMPLETE"


@dataclass(frozen=True)
class BybitDemoTradingRuntimeResult:
    status: BybitDemoTradingRuntimeStatus
    reasons: tuple[str, ...]
    entry_result: BybitDemoResilientAccountSizedCycleResult | None
    managed_poll: BybitDemoManagedTradePollResult | None
    terminal_handoff: BybitDemoTerminalHandoffResult | None
    runtime_lease_acquired: bool
    runtime_lease_released: bool
    next_entry_allowed: bool
    same_invocation_additional_entry_allowed: bool = False
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


class BybitDemoRuntimeLeaseStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    automatic_stale_takeover_allowed: bool

    def acquire(self) -> BybitDemoRuntimeLease: ...

    def release(self, *, owner_token: str) -> None: ...


EntryExecutor = Callable[..., BybitDemoResilientAccountSizedCycleResult]
ManagedPoller = Callable[..., BybitDemoManagedTradePollResult]
TerminalHandoff = Callable[..., BybitDemoTerminalHandoffResult]


def run_bybit_demo_trading_runtime(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    now_ms: int,
    client: Any,
    accounting_client: Any | None,
    excursion_store: BybitDemoExcursionStore,
    completed_bar_client: Any,
    quote_client: Any,
    runtime_lease: BybitDemoRuntimeLeaseStore,
    terminal_evidence_store: Any | None = None,
    managed_policy: BybitDemoManagedTradePollPolicy | None = None,
    entry_executor: EntryExecutor = (
        execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle
    ),
    managed_poller: ManagedPoller = poll_bybit_demo_managed_trade,
    terminal_handoff: TerminalHandoff = (
        persist_and_acknowledge_bybit_demo_terminal_evidence
    ),
    **entry_kwargs: Any,
) -> BybitDemoTradingRuntimeResult:
    """Route one canonical demo invocation to entry *or* active-trade management.

    An exclusive runtime lease closes the race where two processes both observe no excursion
    checkpoint and submit separate entries. Under that lease, a valid active checkpoint always
    wins over new signal selection. Only a missing checkpoint can reach the existing resilient
    entry path. Corrupt checkpoint state blocks trading instead of being treated as no position.

    Fully reconciled terminal evidence can be persisted and acknowledged in the same invocation,
    but even a successful handoff never starts a replacement trade before the lease is released.
    A later invocation may attempt a new entry after it independently observes no checkpoint.
    """

    strategy_config.validate()
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("demo trading runtime now_ms must be a non-negative integer")
    _validate_dependencies(
        excursion_store=excursion_store,
        runtime_lease=runtime_lease,
        client=client,
        quote_client=quote_client,
        completed_bar_client=completed_bar_client,
        terminal_evidence_store=terminal_evidence_store,
    )

    try:
        lease = runtime_lease.acquire()
    except FileExistsError:
        return _result(
            BybitDemoTradingRuntimeStatus.RUNTIME_BUSY,
            reasons=("ANOTHER_DEMO_TRADING_RUNTIME_INVOCATION_HOLDS_LEASE",),
            runtime_lease_acquired=False,
            runtime_lease_released=False,
        )
    except Exception as exc:  # noqa: BLE001 - malformed/unreadable lease is fail-closed.
        return _result(
            BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED,
            reasons=(f"DEMO_RUNTIME_LEASE_ACQUIRE_FAILED:{type(exc).__name__}",),
            runtime_lease_acquired=False,
            runtime_lease_released=False,
        )
    _reject_live_result(lease, name="runtime lease")

    try:
        operation = _run_under_lease(
            bars_by_symbol,
            instruments=instruments,
            strategy_config=strategy_config,
            session_state=session_state,
            now=now,
            now_ms=now_ms,
            client=client,
            accounting_client=accounting_client,
            excursion_store=excursion_store,
            completed_bar_client=completed_bar_client,
            quote_client=quote_client,
            terminal_evidence_store=terminal_evidence_store,
            managed_policy=managed_policy,
            entry_executor=entry_executor,
            managed_poller=managed_poller,
            terminal_handoff=terminal_handoff,
            entry_kwargs=entry_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - unexpected runtime errors must not escape the lease.
        operation = _result(
            BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED,
            reasons=(f"DEMO_TRADING_RUNTIME_OPERATION_FAILED:{type(exc).__name__}",),
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )

    try:
        runtime_lease.release(owner_token=lease.owner_token)
    except Exception as exc:  # noqa: BLE001 - uncertain lease ownership blocks subsequent trading.
        return _result(
            BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED,
            reasons=(f"DEMO_RUNTIME_LEASE_RELEASE_FAILED:{type(exc).__name__}",),
            entry_result=operation.entry_result,
            managed_poll=operation.managed_poll,
            terminal_handoff=operation.terminal_handoff,
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )
    return _result(
        operation.status,
        reasons=operation.reasons,
        entry_result=operation.entry_result,
        managed_poll=operation.managed_poll,
        terminal_handoff=operation.terminal_handoff,
        runtime_lease_acquired=True,
        runtime_lease_released=True,
        next_entry_allowed=operation.next_entry_allowed,
    )


def _run_under_lease(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    now_ms: int,
    client: Any,
    accounting_client: Any | None,
    excursion_store: BybitDemoExcursionStore,
    completed_bar_client: Any,
    quote_client: Any,
    terminal_evidence_store: Any | None,
    managed_policy: BybitDemoManagedTradePollPolicy | None,
    entry_executor: EntryExecutor,
    managed_poller: ManagedPoller,
    terminal_handoff: TerminalHandoff,
    entry_kwargs: Mapping[str, Any],
) -> BybitDemoTradingRuntimeResult:
    try:
        checkpoint = excursion_store.load()
    except FileNotFoundError:
        checkpoint = None
    except Exception as exc:  # noqa: BLE001 - corrupt durable state must never become a new entry.
        return _result(
            BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED,
            reasons=(f"ACTIVE_DEMO_EXCURSION_LOAD_FAILED:{type(exc).__name__}",),
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )

    if checkpoint is None:
        entry = entry_executor(
            bars_by_symbol,
            instruments=instruments,
            strategy_config=strategy_config,
            session_state=session_state,
            now=now,
            client=client,
            accounting_client=accounting_client,
            excursion_store=excursion_store,
            quote_client=quote_client,
            **dict(entry_kwargs),
        )
        _reject_live_result(entry, name="entry cycle")
        return _result(
            BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED,
            entry_result=entry,
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )

    symbol = checkpoint.state.symbol
    instrument = instruments.get(symbol)
    if instrument is None:
        return _result(
            BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED,
            reasons=("ACTIVE_DEMO_EXCURSION_INSTRUMENT_MISSING",),
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )

    managed = managed_poller(
        excursion_store=excursion_store,
        trade_client=client,
        completed_bar_client=completed_bar_client,
        quote_client=quote_client,
        instrument=instrument,
        strategy_config=strategy_config,
        now_ms=now_ms,
        accounting_client=accounting_client,
        policy=managed_policy,
    )
    _reject_live_result(managed, name="managed trade poll")

    if managed.phase is BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY:
        if terminal_evidence_store is None:
            return _result(
                BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED,
                reasons=("TERMINAL_EVIDENCE_STORE_REQUIRED_FOR_FINAL_HANDOFF",),
                managed_poll=managed,
                runtime_lease_acquired=True,
                runtime_lease_released=False,
            )
        handoff = terminal_handoff(
            managed,
            evidence_store=terminal_evidence_store,
            excursion_store=excursion_store,
        )
        _reject_live_result(handoff, name="terminal handoff")
        if handoff.status is BybitDemoTerminalHandoffStatus.COMPLETE:
            return _result(
                BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE,
                managed_poll=managed,
                terminal_handoff=handoff,
                runtime_lease_acquired=True,
                runtime_lease_released=False,
                next_entry_allowed=True,
            )
        return _result(
            BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED,
            reasons=handoff.reasons,
            managed_poll=managed,
            terminal_handoff=handoff,
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )

    return _result(
        BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED,
        reasons=managed.reasons,
        managed_poll=managed,
        runtime_lease_acquired=True,
        runtime_lease_released=False,
    )


def _validate_dependencies(
    *,
    excursion_store: BybitDemoExcursionStore,
    runtime_lease: BybitDemoRuntimeLeaseStore,
    client: Any,
    quote_client: Any,
    completed_bar_client: Any,
    terminal_evidence_store: Any | None,
) -> None:
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo trading runtime rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("demo trading runtime requires diagnostics-only excursion store")
    if runtime_lease.live_mainnet_order_routing_allowed or runtime_lease.order_writes_supported:
        raise ValueError("demo trading runtime rejected unsafe runtime lease")
    if runtime_lease.automatic_stale_takeover_allowed:
        raise ValueError("demo trading runtime forbids automatic runtime lease takeover")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo trading runtime rejected mainnet-capable trade client")
    if getattr(quote_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo trading runtime rejected mainnet-capable quote client")
    if getattr(completed_bar_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo trading runtime rejected mainnet-capable completed-bar client")
    if terminal_evidence_store is not None:
        if (
            getattr(
                terminal_evidence_store,
                "live_mainnet_order_routing_allowed",
                True,
            )
            is not False
        ):
            raise ValueError("demo trading runtime rejected mainnet-capable evidence store")
        if getattr(terminal_evidence_store, "order_writes_supported", True) is not False:
            raise ValueError("demo trading runtime requires diagnostics-only evidence store")


def _reject_live_result(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"demo trading runtime rejected mainnet-capable {name}")


def _result(
    status: BybitDemoTradingRuntimeStatus,
    *,
    reasons: tuple[str, ...] = (),
    entry_result: BybitDemoResilientAccountSizedCycleResult | None = None,
    managed_poll: BybitDemoManagedTradePollResult | None = None,
    terminal_handoff: BybitDemoTerminalHandoffResult | None = None,
    runtime_lease_acquired: bool,
    runtime_lease_released: bool,
    next_entry_allowed: bool = False,
) -> BybitDemoTradingRuntimeResult:
    return BybitDemoTradingRuntimeResult(
        status=status,
        reasons=reasons,
        entry_result=entry_result,
        managed_poll=managed_poll,
        terminal_handoff=terminal_handoff,
        runtime_lease_acquired=runtime_lease_acquired,
        runtime_lease_released=runtime_lease_released,
        next_entry_allowed=next_entry_allowed,
        same_invocation_additional_entry_allowed=False,
    )
