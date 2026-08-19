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
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
)


class BybitDemoTerminalHandoffStatus(StrEnum):
    NOT_READY = "NOT_READY"
    EVIDENCE_PERSIST_FAILED = "EVIDENCE_PERSIST_FAILED"
    EVIDENCE_PERSISTED_ACK_FAILED = "EVIDENCE_PERSISTED_ACK_FAILED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class BybitDemoTerminalHandoffResult:
    status: BybitDemoTerminalHandoffStatus
    reasons: tuple[str, ...]
    receipt: BybitDemoTerminalEvidenceReceipt | None
    acknowledgement: BybitDemoExcursionRuntimeResult | None
    evidence_durable: bool
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


def persist_and_acknowledge_bybit_demo_terminal_evidence(
    poll: BybitDemoManagedTradePollResult,
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    excursion_store: BybitDemoExcursionStore,
    acknowledge: Any = acknowledge_bybit_demo_excursion_final,
) -> BybitDemoTerminalHandoffResult:
    """Durably persist final MFE-to-all-in evidence before clearing the active checkpoint.

    This is a two-phase crash-safe handoff. Repeating it after a crash between persistence and
    checkpoint clear is safe because terminal evidence persistence is immutable and idempotent.
    The original symbol is reusable only after fully reconciled accounting, durable evidence and
    an acknowledgement that clears the exact terminal excursion revision.
    """

    _validate_dependencies(evidence_store=evidence_store, excursion_store=excursion_store)
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
        ack = acknowledge(
            store=excursion_store,
            expected_revision=checkpoint.revision,
        )
    except Exception as exc:  # noqa: BLE001 - durable evidence makes later ACK retry safe.
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED,
            reasons=(f"TERMINAL_EXCURSION_ACK_FAILED:{type(exc).__name__}",),
            receipt=receipt,
            evidence_durable=True,
        )
    _reject_live_result(ack, name="terminal excursion acknowledgement")
    if ack.status is not BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED:
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED,
            reasons=ack.reasons or ("TERMINAL_EXCURSION_ACK_NOT_CONFIRMED",),
            receipt=receipt,
            acknowledgement=ack,
            evidence_durable=True,
        )

    return _result(
        BybitDemoTerminalHandoffStatus.COMPLETE,
        receipt=receipt,
        acknowledgement=ack,
        evidence_durable=True,
        checkpoint_cleared=True,
        next_entry_allowed=True,
    )


def _validate_dependencies(
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    excursion_store: BybitDemoExcursionStore,
) -> None:
    if evidence_store.live_mainnet_order_routing_allowed:
        raise ValueError("terminal handoff rejected mainnet-capable evidence store")
    if evidence_store.order_writes_supported or not evidence_store.immutable_records:
        raise ValueError("terminal handoff requires immutable diagnostics-only evidence store")
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
    acknowledgement: BybitDemoExcursionRuntimeResult | None = None,
    evidence_durable: bool = False,
    checkpoint_cleared: bool = False,
    next_entry_allowed: bool = False,
) -> BybitDemoTerminalHandoffResult:
    return BybitDemoTerminalHandoffResult(
        status=status,
        reasons=reasons,
        receipt=receipt,
        acknowledgement=acknowledgement,
        evidence_durable=evidence_durable,
        checkpoint_cleared=checkpoint_cleared,
        next_entry_allowed=next_entry_allowed,
    )
