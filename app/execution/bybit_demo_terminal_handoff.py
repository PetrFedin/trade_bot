from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    BybitDemoExcursionRuntimeStatus,
    acknowledge_bybit_demo_excursion_final,
)
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    BybitDemoManagedTradePollResult,
)
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_session_risk_runtime import (
    BybitDemoSessionRiskCommitReceipt,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
)


class BybitDemoTerminalHandoffStatus(StrEnum):
    NOT_READY = "NOT_READY"
    EVIDENCE_PERSIST_FAILED = "EVIDENCE_PERSIST_FAILED"
    SESSION_RISK_PERSIST_FAILED = "SESSION_RISK_PERSIST_FAILED"
    DURABLE_TERMINAL_STATE_ACK_FAILED = "DURABLE_TERMINAL_STATE_ACK_FAILED"
    EVIDENCE_PERSISTED_ACK_FAILED = "EVIDENCE_PERSISTED_ACK_FAILED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class BybitDemoTerminalHandoffResult:
    status: BybitDemoTerminalHandoffStatus
    reasons: tuple[str, ...]
    receipt: BybitDemoTerminalEvidenceReceipt | None
    session_risk_receipt: BybitDemoSessionRiskCommitReceipt | None
    acknowledgement: BybitDemoExcursionRuntimeResult | None
    evidence_durable: bool
    session_risk_durable: bool
    checkpoint_cleared: bool
    next_entry_allowed: bool
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


class BybitDemoTerminalEvidenceStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    immutable_records: bool

    def persist(
        self,
        *,
        entry_order_link_id: str,
        checkpoint_revision: str,
        evidence: BybitDemoProfitPreservationEvidence,
    ) -> BybitDemoTerminalEvidenceReceipt: ...


class BybitDemoSessionRiskCommitter(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    automatic_reset_allowed: bool
    initialized_session_required: bool

    def commit(self, accounting: Any) -> BybitDemoSessionRiskCommitReceipt: ...


def persist_and_acknowledge_bybit_demo_terminal_evidence(
    poll: BybitDemoManagedTradePollResult,
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    session_risk_committer: BybitDemoSessionRiskCommitter,
    excursion_store: BybitDemoExcursionStore,
    acknowledge: Any = acknowledge_bybit_demo_excursion_final,
) -> BybitDemoTerminalHandoffResult:
    """Commit all durable terminal state before clearing the active checkpoint.

    Crash-safe ordering is strict: immutable terminal evidence -> durable v122 session-risk update
    -> exact excursion acknowledgement. A failure in either durable phase leaves the active
    checkpoint in place. Repeating the handoff is safe because both durable writes are immutable
    and idempotent for the same terminal economics.
    """

    _validate_dependencies(
        evidence_store=evidence_store,
        session_risk_committer=session_risk_committer,
        excursion_store=excursion_store,
    )
    if poll.live_mainnet_order_routing_allowed:
        raise ValueError("terminal handoff rejected mainnet-capable managed poll")
    if (
        poll.phase is not BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY
        or not poll.fully_reconciled_all_in
        or poll.profit_evidence is None
        or poll.accounting is None
        or poll.excursion.checkpoint is None
        or not poll.terminal_evidence_ack_required
    ):
        return _result(
            BybitDemoTerminalHandoffStatus.NOT_READY,
            reasons=("FULLY_RECONCILED_TERMINAL_EVIDENCE_NOT_READY",),
        )
    if not poll.accounting.lifecycle.next_entry_allowed:
        return _result(
            BybitDemoTerminalHandoffStatus.NOT_READY,
            reasons=("ACCOUNTING_LIFECYCLE_DOES_NOT_ALLOW_SYMBOL_REUSE",),
        )
    if not poll.profit_evidence.fully_reconciled_all_in:
        return _result(
            BybitDemoTerminalHandoffStatus.NOT_READY,
            reasons=("PROFIT_EVIDENCE_IS_NOT_FULLY_RECONCILED",),
        )

    checkpoint = poll.excursion.checkpoint
    try:
        receipt = evidence_store.persist(
            entry_order_link_id=checkpoint.entry_order_link_id,
            checkpoint_revision=checkpoint.revision,
            evidence=poll.profit_evidence,
        )
    except Exception as exc:  # noqa: BLE001 - checkpoint must remain until evidence is durable.
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSIST_FAILED,
            reasons=(f"TERMINAL_EVIDENCE_PERSIST_FAILED:{type(exc).__name__}",),
        )
    _reject_live_result(receipt, name="terminal evidence receipt")

    try:
        risk_receipt = session_risk_committer.commit(poll.accounting)
    except Exception as exc:  # noqa: BLE001 - evidence is durable; checkpoint must remain for retry.
        return _result(
            BybitDemoTerminalHandoffStatus.SESSION_RISK_PERSIST_FAILED,
            reasons=(f"SESSION_RISK_PERSIST_FAILED:{type(exc).__name__}",),
            receipt=receipt,
            evidence_durable=True,
        )
    _reject_live_result(risk_receipt, name="session-risk commit receipt")
    if risk_receipt.entry_order_link_id != checkpoint.entry_order_link_id:
        return _result(
            BybitDemoTerminalHandoffStatus.SESSION_RISK_PERSIST_FAILED,
            reasons=("SESSION_RISK_COMMIT_ENTRY_ID_MISMATCH",),
            receipt=receipt,
            session_risk_receipt=risk_receipt,
            evidence_durable=True,
        )

    try:
        ack = acknowledge(
            store=excursion_store,
            expected_revision=checkpoint.revision,
        )
    except Exception as exc:  # noqa: BLE001 - both durable commits make later ACK retry safe.
        return _result(
            BybitDemoTerminalHandoffStatus.DURABLE_TERMINAL_STATE_ACK_FAILED,
            reasons=(f"TERMINAL_EXCURSION_ACK_FAILED:{type(exc).__name__}",),
            receipt=receipt,
            session_risk_receipt=risk_receipt,
            evidence_durable=True,
            session_risk_durable=True,
        )
    _reject_live_result(ack, name="terminal excursion acknowledgement")
    if ack.status is not BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED:
        return _result(
            BybitDemoTerminalHandoffStatus.DURABLE_TERMINAL_STATE_ACK_FAILED,
            reasons=ack.reasons or ("TERMINAL_EXCURSION_ACK_NOT_CONFIRMED",),
            receipt=receipt,
            session_risk_receipt=risk_receipt,
            acknowledgement=ack,
            evidence_durable=True,
            session_risk_durable=True,
        )

    return _result(
        BybitDemoTerminalHandoffStatus.COMPLETE,
        receipt=receipt,
        session_risk_receipt=risk_receipt,
        acknowledgement=ack,
        evidence_durable=True,
        session_risk_durable=True,
        checkpoint_cleared=True,
        next_entry_allowed=True,
    )


def _validate_dependencies(
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    session_risk_committer: BybitDemoSessionRiskCommitter,
    excursion_store: BybitDemoExcursionStore,
) -> None:
    if evidence_store.live_mainnet_order_routing_allowed:
        raise ValueError("terminal handoff rejected mainnet-capable evidence store")
    if evidence_store.order_writes_supported or not evidence_store.immutable_records:
        raise ValueError("terminal handoff requires immutable diagnostics-only evidence store")
    if session_risk_committer.live_mainnet_order_routing_allowed:
        raise ValueError("terminal handoff rejected mainnet-capable session-risk committer")
    if session_risk_committer.order_writes_supported:
        raise ValueError("terminal handoff requires diagnostics-only session-risk committer")
    if session_risk_committer.automatic_reset_allowed:
        raise ValueError("terminal handoff forbids automatic session-risk reset")
    if not session_risk_committer.initialized_session_required:
        raise ValueError("terminal handoff requires an explicitly initialized risk session")
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("terminal handoff rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("terminal handoff requires diagnostics-only excursion store")


def _reject_live_result(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"terminal handoff rejected mainnet-capable {name}")


def _result(
    status: BybitDemoTerminalHandoffStatus,
    *,
    reasons: tuple[str, ...] = (),
    receipt: BybitDemoTerminalEvidenceReceipt | None = None,
    session_risk_receipt: BybitDemoSessionRiskCommitReceipt | None = None,
    acknowledgement: BybitDemoExcursionRuntimeResult | None = None,
    evidence_durable: bool = False,
    session_risk_durable: bool = False,
    checkpoint_cleared: bool = False,
    next_entry_allowed: bool = False,
) -> BybitDemoTerminalHandoffResult:
    return BybitDemoTerminalHandoffResult(
        status=status,
        reasons=reasons,
        receipt=receipt,
        session_risk_receipt=session_risk_receipt,
        acknowledgement=acknowledgement,
        evidence_durable=evidence_durable,
        session_risk_durable=session_risk_durable,
        checkpoint_cleared=checkpoint_cleared,
        next_entry_allowed=next_entry_allowed,
    )
