from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    BybitDemoExcursionRuntimeStatus,
    advance_bybit_demo_excursion_tracking,
)
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_max_hold_close import (
    BybitDemoMaxHoldClosePolicy,
    BybitDemoMaxHoldCloseResult,
    execute_bybit_demo_max_hold_close,
)
from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
    reconcile_bybit_demo_post_trade_accounting,
)
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
    build_bybit_demo_profit_preservation_evidence,
)
from app.execution.bybit_demo_session_risk_flatten import (
    BybitDemoSessionRiskFlattenPolicy,
    BybitDemoSessionRiskFlattenResult,
    execute_bybit_demo_session_risk_flatten,
)
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimePolicy,
    BybitDemoTradeManagementRuntimeResult,
    BybitDemoTradeManagementRuntimeStatus,
    run_bybit_demo_trade_management_cycle,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState, evaluate_crypto_session_risk


class BybitDemoManagedTradePollPhase(StrEnum):
    TRACKING_BLOCKED = "TRACKING_BLOCKED"
    OPEN_MANAGED = "OPEN_MANAGED"
    CLOSE_RECONCILIATION_REQUIRED = "CLOSE_RECONCILIATION_REQUIRED"
    SESSION_RISK_ACTION = "SESSION_RISK_ACTION"
    MAX_HOLD_ACTION = "MAX_HOLD_ACTION"
    TERMINAL_ACCOUNTING_PENDING = "TERMINAL_ACCOUNTING_PENDING"
    TERMINAL_EVIDENCE_READY = "TERMINAL_EVIDENCE_READY"


@dataclass(frozen=True)
class BybitDemoManagedTradePollPolicy:
    trade_management: BybitDemoTradeManagementRuntimePolicy = field(
        default_factory=BybitDemoTradeManagementRuntimePolicy
    )
    max_hold_close: BybitDemoMaxHoldClosePolicy = field(
        default_factory=BybitDemoMaxHoldClosePolicy
    )
    session_risk_flatten: BybitDemoSessionRiskFlattenPolicy = field(
        default_factory=BybitDemoSessionRiskFlattenPolicy
    )

    def validate(self) -> None:
        self.trade_management.validate()
        self.max_hold_close.validate()
        self.session_risk_flatten.validate()


@dataclass(frozen=True)
class BybitDemoManagedTradePollResult:
    phase: BybitDemoManagedTradePollPhase
    reasons: tuple[str, ...]
    excursion: BybitDemoExcursionRuntimeResult
    management: BybitDemoTradeManagementRuntimeResult | None
    max_hold_close: BybitDemoMaxHoldCloseResult | None
    session_risk_flatten: BybitDemoSessionRiskFlattenResult | None
    accounting: BybitDemoPostTradeAccountingResult | None
    profit_evidence: BybitDemoProfitPreservationEvidence | None
    terminal_evidence_ack_required: bool
    fully_reconciled_all_in: bool
    next_entry_allowed: bool = False
    demo_only: bool = True
    automatic_strategy_activation_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


AdvanceExcursion = Callable[..., BybitDemoExcursionRuntimeResult]
RunManagement = Callable[..., BybitDemoTradeManagementRuntimeResult]
RunMaxHoldClose = Callable[..., BybitDemoMaxHoldCloseResult]
RunSessionRiskFlatten = Callable[..., BybitDemoSessionRiskFlattenResult]
RunAccounting = Callable[..., BybitDemoPostTradeAccountingResult]
BuildProfitEvidence = Callable[..., BybitDemoProfitPreservationEvidence]


def poll_bybit_demo_managed_trade(
    *,
    excursion_store: BybitDemoExcursionStore,
    trade_client: Any,
    completed_bar_client: Any,
    quote_client: Any,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    now_ms: int,
    accounting_client: Any | None = None,
    funding_ledger: Any | None = None,
    session_state: CryptoSessionRiskState | None = None,
    policy: BybitDemoManagedTradePollPolicy | None = None,
    advance_excursion: AdvanceExcursion = advance_bybit_demo_excursion_tracking,
    run_management: RunManagement = run_bybit_demo_trade_management_cycle,
    run_max_hold_close: RunMaxHoldClose = execute_bybit_demo_max_hold_close,
    run_session_risk_flatten: RunSessionRiskFlatten = execute_bybit_demo_session_risk_flatten,
    run_accounting: RunAccounting = reconcile_bybit_demo_post_trade_accounting,
    build_profit_evidence: BuildProfitEvidence = build_bybit_demo_profit_preservation_evidence,
) -> BybitDemoManagedTradePollResult:
    """Advance one already-open Demo trade through risk, management or terminal accounting.

    Execution/fill reconciliation is authoritative. A terminal trade never receives another stop
    update. For an open trade, a durable session-risk flatten requirement has first priority and
    suppresses normal ratchet/max-hold management until the risk-reduction action is resolved. The
    session-risk close is reduce-only and independently reconciled. Stop, max-hold and session-risk
    writes remain disabled by default and must be enabled explicitly by the operational product.

    Terminal excursion state is deliberately not acknowledged or cleared here: callers must first
    persist final evidence and v122 session risk, then use the explicit final-ack API. Until that
    durable handoff happens, this poll never permits symbol reuse.
    """

    active = BybitDemoManagedTradePollPolicy() if policy is None else policy
    active.validate()
    instrument.validate()
    strategy_config.validate()
    if isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("managed demo trade poll now_ms must be non-negative")

    excursion = advance_excursion(
        store=excursion_store,
        trade_client=trade_client,
        quote_client=quote_client,
        execution_limit=active.trade_management.execution_limit,
    )
    _reject_live_result(excursion, name="excursion")

    if excursion.status is BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED:
        return _result(
            BybitDemoManagedTradePollPhase.TRACKING_BLOCKED,
            excursion=excursion,
            reasons=excursion.reasons,
        )

    if excursion.status is BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY:
        return _terminal_result(
            excursion,
            accounting_client=accounting_client,
            funding_ledger=funding_ledger,
            run_accounting=run_accounting,
            build_profit_evidence=build_profit_evidence,
        )

    if excursion.status is not BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED:
        return _result(
            BybitDemoManagedTradePollPhase.TRACKING_BLOCKED,
            excursion=excursion,
            reasons=(f"MANAGED_POLL_UNEXPECTED_EXCURSION_STATUS:{excursion.status.value}",),
        )

    if session_state is not None:
        risk = evaluate_crypto_session_risk(session_state)
        if risk.flatten_required:
            flatten = run_session_risk_flatten(
                session_state=session_state,
                excursion_store=excursion_store,
                client=trade_client,
                quote_client=quote_client,
                instrument=instrument,
                policy=active.session_risk_flatten,
            )
            _reject_live_result(flatten, name="session-risk flatten")
            reasons = tuple(dict.fromkeys((*risk.reasons, *flatten.reasons)))
            return _result(
                BybitDemoManagedTradePollPhase.SESSION_RISK_ACTION,
                excursion=excursion,
                session_risk_flatten=flatten,
                reasons=reasons,
            )

    management = run_management(
        excursion_store=excursion_store,
        client=trade_client,
        completed_bar_client=completed_bar_client,
        quote_client=quote_client,
        instrument=instrument,
        strategy_config=strategy_config,
        now_ms=now_ms,
        runtime_policy=active.trade_management,
    )
    _reject_live_result(management, name="management")

    if management.status is BybitDemoTradeManagementRuntimeStatus.POSITION_CLOSED:
        return _result(
            BybitDemoManagedTradePollPhase.CLOSE_RECONCILIATION_REQUIRED,
            excursion=excursion,
            management=management,
            reasons=management.reasons,
        )

    if management.status is BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED:
        max_hold = run_max_hold_close(
            management,
            excursion_store=excursion_store,
            client=trade_client,
            quote_client=quote_client,
            instrument=instrument,
            policy=active.max_hold_close,
        )
        _reject_live_result(max_hold, name="max-hold close")
        reasons = tuple(dict.fromkeys((*management.reasons, *max_hold.reasons)))
        return _result(
            BybitDemoManagedTradePollPhase.MAX_HOLD_ACTION,
            excursion=excursion,
            management=management,
            max_hold_close=max_hold,
            reasons=reasons,
        )

    if management.status in {
        BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
        BybitDemoTradeManagementRuntimeStatus.RATCHET_WRITE_FAILED,
        BybitDemoTradeManagementRuntimeStatus.RATCHET_UNVERIFIED,
    }:
        return _result(
            BybitDemoManagedTradePollPhase.TRACKING_BLOCKED,
            excursion=excursion,
            management=management,
            reasons=management.reasons,
        )

    return _result(
        BybitDemoManagedTradePollPhase.OPEN_MANAGED,
        excursion=excursion,
        management=management,
        reasons=management.reasons,
    )


def _terminal_result(
    excursion: BybitDemoExcursionRuntimeResult,
    *,
    accounting_client: Any | None,
    funding_ledger: Any | None,
    run_accounting: RunAccounting,
    build_profit_evidence: BuildProfitEvidence,
) -> BybitDemoManagedTradePollResult:
    if excursion.trade is None or excursion.final is None or excursion.checkpoint is None:
        return _result(
            BybitDemoManagedTradePollPhase.TRACKING_BLOCKED,
            excursion=excursion,
            reasons=("TERMINAL_EXCURSION_EVIDENCE_INCOMPLETE",),
            terminal_ack_required=True,
        )
    if not excursion.checkpoint_clear_allowed:
        return _result(
            BybitDemoManagedTradePollPhase.TRACKING_BLOCKED,
            excursion=excursion,
            reasons=("TERMINAL_EXCURSION_CLEAR_NOT_ALLOWED",),
            terminal_ack_required=True,
        )
    if accounting_client is None:
        return _result(
            BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING,
            excursion=excursion,
            reasons=("TERMINAL_ACCOUNTING_CLIENT_REQUIRED",),
            terminal_ack_required=True,
        )

    try:
        accounting = run_accounting(
            excursion.trade,
            client=accounting_client,
            funding_ledger=funding_ledger,
        )
    except Exception as exc:  # noqa: BLE001 - terminal checkpoint must survive read failures.
        return _result(
            BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING,
            excursion=excursion,
            reasons=(f"TERMINAL_ACCOUNTING_FAILED:{type(exc).__name__}",),
            terminal_ack_required=True,
        )
    _reject_live_result(accounting, name="accounting")

    try:
        evidence = build_profit_evidence(excursion.final, accounting)
    except Exception as exc:  # noqa: BLE001 - terminal checkpoint must survive evidence failures.
        return _result(
            BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING,
            excursion=excursion,
            accounting=accounting,
            reasons=(f"TERMINAL_PROFIT_EVIDENCE_FAILED:{type(exc).__name__}",),
            terminal_ack_required=True,
        )
    _reject_live_result(evidence, name="profit evidence")

    fully_reconciled = evidence.fully_reconciled_all_in
    phase = (
        BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY
        if fully_reconciled
        else BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING
    )
    reasons = () if fully_reconciled else ("TERMINAL_ALL_IN_ACCOUNTING_PENDING",)
    return _result(
        phase,
        excursion=excursion,
        accounting=accounting,
        profit_evidence=evidence,
        reasons=reasons,
        terminal_ack_required=True,
        fully_reconciled=fully_reconciled,
    )


def _reject_live_result(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"managed demo trade poll rejected mainnet-capable {name} result")


def _result(
    phase: BybitDemoManagedTradePollPhase,
    *,
    excursion: BybitDemoExcursionRuntimeResult,
    reasons: tuple[str, ...] = (),
    management: BybitDemoTradeManagementRuntimeResult | None = None,
    max_hold_close: BybitDemoMaxHoldCloseResult | None = None,
    session_risk_flatten: BybitDemoSessionRiskFlattenResult | None = None,
    accounting: BybitDemoPostTradeAccountingResult | None = None,
    profit_evidence: BybitDemoProfitPreservationEvidence | None = None,
    terminal_ack_required: bool = False,
    fully_reconciled: bool = False,
) -> BybitDemoManagedTradePollResult:
    return BybitDemoManagedTradePollResult(
        phase=phase,
        reasons=reasons,
        excursion=excursion,
        management=management,
        max_hold_close=max_hold_close,
        session_risk_flatten=session_risk_flatten,
        accounting=accounting,
        profit_evidence=profit_evidence,
        terminal_evidence_ack_required=terminal_ack_required,
        fully_reconciled_all_in=fully_reconciled,
        next_entry_allowed=False,
    )
