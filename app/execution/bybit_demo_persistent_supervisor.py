from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Any

from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPolicy,
    poll_bybit_demo_managed_trade,
)
from app.execution.bybit_demo_session_risk_runtime import (
    BybitDemoSessionRiskObservation,
    PostgresBybitDemoSessionRiskCommitter,
    PostgresBybitDemoSessionRiskObserver,
)
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeResult,
    BybitDemoTradingRuntimeStatus,
    run_bybit_demo_trading_runtime,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig


class BybitDemoPersistentSupervisorStatus(StrEnum):
    IDLE_NO_ACTIVE_TRADE = "IDLE_NO_ACTIVE_TRADE"
    ACTIVE_TRADE_CYCLE = "ACTIVE_TRADE_CYCLE"
    TERMINAL_HANDOFF_COMPLETE = "TERMINAL_HANDOFF_COMPLETE"
    RUNTIME_BUSY = "RUNTIME_BUSY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitDemoPersistentSupervisorResult:
    status: BybitDemoPersistentSupervisorStatus
    reasons: tuple[str, ...]
    active_symbol: str | None
    runtime: BybitDemoTradingRuntimeResult | None
    session_risk: BybitDemoSessionRiskObservation | None
    new_entry_attempted: bool
    next_entry_allowed: bool = False
    persistent_management_supported: bool = True
    autonomous_entry_allowed: bool = False
    operator_approval_bypass_allowed: bool = False
    same_invocation_additional_entry_allowed: bool = False
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def run_bybit_demo_persistent_supervisor_cycle(
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    now: datetime,
    now_ms: int,
    client: Any,
    accounting_client: Any,
    excursion_store: Any,
    completed_bar_client: Any,
    quote_client: Any,
    runtime_lease: Any,
    terminal_evidence_store: Any,
    session_risk_committer: PostgresBybitDemoSessionRiskCommitter,
    session_risk_observer: PostgresBybitDemoSessionRiskObserver,
    managed_policy: BybitDemoManagedTradePollPolicy | None = None,
    canonical_runtime: Any = run_bybit_demo_trading_runtime,
) -> BybitDemoPersistentSupervisorResult:
    """Run one restart-safe management cycle for an already-open Bybit Demo trade.

    This supervisor intentionally has no entry-authorisation input and never selects a new trade.
    A missing active excursion checkpoint is therefore an IDLE condition, not permission to scan or
    enter. When a checkpoint exists, the supervisor observes real wallet equity into durable v122
    session risk, then delegates the exact active identity to the canonical single-writer runtime.

    A hard-block entry executor is injected as a second line of defence. If another process clears
    the checkpoint between this precheck and canonical lease acquisition, the supervisor blocks
    rather than falling through to new exposure. Terminal completion also never creates a same-call
    replacement entry. New risk remains a separate operator-approved action.
    """

    strategy_config.validate()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("persistent Demo supervisor requires timezone-aware now")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("persistent Demo supervisor now_ms must be a non-negative integer")
    _validate_dependencies(
        client=client,
        accounting_client=accounting_client,
        excursion_store=excursion_store,
        completed_bar_client=completed_bar_client,
        quote_client=quote_client,
        runtime_lease=runtime_lease,
        terminal_evidence_store=terminal_evidence_store,
        session_risk_committer=session_risk_committer,
        session_risk_observer=session_risk_observer,
    )

    try:
        checkpoint = excursion_store.load()
    except FileNotFoundError:
        return _result(BybitDemoPersistentSupervisorStatus.IDLE_NO_ACTIVE_TRADE)
    except Exception as exc:  # noqa: BLE001 - corrupt durable state must fail closed.
        return _result(
            BybitDemoPersistentSupervisorStatus.BLOCKED,
            reasons=(f"PERSISTENT_SUPERVISOR_CHECKPOINT_LOAD_FAILED:{type(exc).__name__}",),
        )

    active_symbol = checkpoint.state.symbol
    instrument = instruments.get(active_symbol)
    if instrument is None:
        return _result(
            BybitDemoPersistentSupervisorStatus.BLOCKED,
            reasons=("PERSISTENT_SUPERVISOR_ACTIVE_INSTRUMENT_MISSING",),
            active_symbol=active_symbol,
        )
    instrument.validate()

    try:
        wallet = accounting_client.get_wallet_balance()
        wallet.validate()
        session_risk = session_risk_observer.observe(
            current_equity_usdt=wallet.total_equity_usd,
        )
    except Exception as exc:  # noqa: BLE001 - never manage against unknown session risk.
        return _result(
            BybitDemoPersistentSupervisorStatus.BLOCKED,
            reasons=(f"PERSISTENT_SUPERVISOR_SESSION_RISK_FAILED:{type(exc).__name__}",),
            active_symbol=active_symbol,
        )
    _reject_live_result(session_risk, name="session-risk observation")

    managed_poller = partial(
        poll_bybit_demo_managed_trade,
        session_state=session_risk.session_state,
    )
    runtime = canonical_runtime(
        {},
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_risk.session_state,
        now=now,
        now_ms=now_ms,
        client=client,
        accounting_client=accounting_client,
        excursion_store=excursion_store,
        completed_bar_client=completed_bar_client,
        quote_client=quote_client,
        runtime_lease=runtime_lease,
        terminal_evidence_store=terminal_evidence_store,
        session_risk_committer=session_risk_committer,
        managed_policy=managed_policy,
        managed_poller=managed_poller,
        entry_executor=_forbid_persistent_supervisor_entry,
    )
    _reject_live_result(runtime, name="canonical runtime")
    if runtime.same_invocation_additional_entry_allowed:
        raise ValueError("persistent Demo supervisor rejected same-invocation replacement entry")
    if runtime.status is BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED:
        raise ValueError("persistent Demo supervisor unexpectedly executed a new entry")
    if runtime.status is BybitDemoTradingRuntimeStatus.RUNTIME_BUSY:
        return _result(
            BybitDemoPersistentSupervisorStatus.RUNTIME_BUSY,
            reasons=runtime.reasons,
            active_symbol=active_symbol,
            runtime=runtime,
            session_risk=session_risk,
        )
    if runtime.status is BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED:
        return _result(
            BybitDemoPersistentSupervisorStatus.BLOCKED,
            reasons=runtime.reasons,
            active_symbol=active_symbol,
            runtime=runtime,
            session_risk=session_risk,
        )
    if runtime.status is BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE:
        return _result(
            BybitDemoPersistentSupervisorStatus.TERMINAL_HANDOFF_COMPLETE,
            reasons=runtime.reasons,
            active_symbol=active_symbol,
            runtime=runtime,
            session_risk=session_risk,
        )
    if runtime.status is not BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED:
        return _result(
            BybitDemoPersistentSupervisorStatus.BLOCKED,
            reasons=(f"PERSISTENT_SUPERVISOR_UNEXPECTED_RUNTIME_STATUS:{runtime.status.value}",),
            active_symbol=active_symbol,
            runtime=runtime,
            session_risk=session_risk,
        )
    return _result(
        BybitDemoPersistentSupervisorStatus.ACTIVE_TRADE_CYCLE,
        reasons=runtime.reasons,
        active_symbol=active_symbol,
        runtime=runtime,
        session_risk=session_risk,
    )


def _forbid_persistent_supervisor_entry(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("persistent Demo supervisor forbids new entry execution")


def _validate_dependencies(
    *,
    client: Any,
    accounting_client: Any,
    excursion_store: Any,
    completed_bar_client: Any,
    quote_client: Any,
    runtime_lease: Any,
    terminal_evidence_store: Any,
    session_risk_committer: Any,
    session_risk_observer: Any,
) -> None:
    if getattr(client, "environment", None) != "BYBIT_DEMO":
        raise ValueError("persistent Demo supervisor requires a BYBIT_DEMO order client")
    for name, value in (
        ("order client", client),
        ("accounting client", accounting_client),
        ("excursion store", excursion_store),
        ("completed-bar client", completed_bar_client),
        ("quote client", quote_client),
        ("runtime lease", runtime_lease),
        ("terminal evidence store", terminal_evidence_store),
        ("session-risk committer", session_risk_committer),
        ("session-risk observer", session_risk_observer),
    ):
        if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError(f"persistent Demo supervisor rejected mainnet-capable {name}")
    if getattr(accounting_client, "order_writes_supported", True) is not False:
        raise ValueError("persistent Demo supervisor requires read-only accounting client")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("persistent Demo supervisor requires diagnostics-only excursion store")
    if getattr(runtime_lease, "order_writes_supported", True) is not False:
        raise ValueError("persistent Demo supervisor requires diagnostics-only runtime lease")
    if getattr(runtime_lease, "automatic_stale_takeover_allowed", True) is not False:
        raise ValueError("persistent Demo supervisor forbids automatic lease takeover")
    if getattr(terminal_evidence_store, "order_writes_supported", True) is not False:
        raise ValueError("persistent Demo supervisor requires diagnostics-only terminal evidence")
    for name, value in (
        ("session-risk committer", session_risk_committer),
        ("session-risk observer", session_risk_observer),
    ):
        if getattr(value, "order_writes_supported", True) is not False:
            raise ValueError(f"persistent Demo supervisor requires diagnostics-only {name}")
        if getattr(value, "automatic_reset_allowed", True) is not False:
            raise ValueError(f"persistent Demo supervisor forbids automatic reset in {name}")


def _reject_live_result(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"persistent Demo supervisor rejected mainnet-capable {name}")


def _result(
    status: BybitDemoPersistentSupervisorStatus,
    *,
    reasons: tuple[str, ...] = (),
    active_symbol: str | None = None,
    runtime: BybitDemoTradingRuntimeResult | None = None,
    session_risk: BybitDemoSessionRiskObservation | None = None,
) -> BybitDemoPersistentSupervisorResult:
    return BybitDemoPersistentSupervisorResult(
        status=status,
        reasons=reasons,
        active_symbol=active_symbol,
        runtime=runtime,
        session_risk=session_risk,
        new_entry_attempted=False,
        next_entry_allowed=False,
    )


__all__ = [
    "BybitDemoPersistentSupervisorResult",
    "BybitDemoPersistentSupervisorStatus",
    "run_bybit_demo_persistent_supervisor_cycle",
]
