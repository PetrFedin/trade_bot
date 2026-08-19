from __future__ import annotations

from typing import Protocol

from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeStatus,
    acknowledge_bybit_demo_excursion_final,
)
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    BybitDemoManagedTradePollResult,
)
from app.execution.bybit_demo_session_risk_ledger import (
    apply_fully_reconciled_trade_to_session_ledger,
)
from app.execution.bybit_demo_terminal_handoff import (
    BybitDemoTerminalEvidenceStore,
    BybitDemoTerminalHandoffResult,
    BybitDemoTerminalHandoffStatus,
)
from app.execution.bybit_postgres_evidence_state import (
    PostgresBybitDemoSessionRiskLedgerStore,
)


class _ExcursionStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def clear(self, *, expected_revision: str) -> None: ...


def persist_product_terminal_state(
    poll: BybitDemoManagedTradePollResult,
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    excursion_store: _ExcursionStore,
    session_risk_store: PostgresBybitDemoSessionRiskLedgerStore,
) -> BybitDemoTerminalHandoffResult:
    """Commit final economics to evidence and session risk before releasing symbol reuse.

    Ordering is deliberate: immutable terminal evidence -> session-risk ledger -> exact excursion
    checkpoint clear. A crash before the final clear leaves the checkpoint active, so the same
    fully reconciled result is retried idempotently instead of allowing a new entry with stale risk.
    """

    _validate_dependencies(
        evidence_store=evidence_store,
        excursion_store=excursion_store,
        session_risk_store=session_risk_store,
    )
    if poll.live_mainnet_order_routing_allowed:
        raise ValueError("product terminal handoff rejected mainnet-capable managed poll")
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
    except Exception as exc:  # noqa: BLE001 - active checkpoint must remain.
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSIST_FAILED,
            reasons=(f"TERMINAL_EVIDENCE_PERSIST_FAILED:{type(exc).__name__}",),
        )
    _reject_live_result(receipt, name="terminal evidence receipt")

    try:
        session_checkpoint = session_risk_store.load_current()
        updated_ledger = apply_fully_reconciled_trade_to_session_ledger(
            session_checkpoint.ledger,
            poll.accounting,
        )
        if updated_ledger != session_checkpoint.ledger:
            session_risk_store.save(
                updated_ledger,
                expected_revision=session_checkpoint.revision,
            )
    except Exception as exc:  # noqa: BLE001 - durable evidence makes exact retry safe.
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED,
            reasons=(f"SESSION_RISK_PERSIST_FAILED:{type(exc).__name__}",),
            receipt=receipt,
            evidence_durable=True,
        )

    try:
        acknowledgement = acknowledge_bybit_demo_excursion_final(
            store=excursion_store,
            expected_revision=checkpoint.revision,
        )
    except Exception as exc:  # noqa: BLE001 - risk/evidence are durable, checkpoint stays active.
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED,
            reasons=(f"TERMINAL_EXCURSION_ACK_FAILED:{type(exc).__name__}",),
            receipt=receipt,
            evidence_durable=True,
        )
    _reject_live_result(acknowledgement, name="terminal excursion acknowledgement")
    if acknowledgement.status is not BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED:
        return _result(
            BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED,
            reasons=acknowledgement.reasons or ("TERMINAL_EXCURSION_ACK_NOT_CONFIRMED",),
            receipt=receipt,
            acknowledgement=acknowledgement,
            evidence_durable=True,
        )

    return _result(
        BybitDemoTerminalHandoffStatus.COMPLETE,
        receipt=receipt,
        acknowledgement=acknowledgement,
        evidence_durable=True,
        checkpoint_cleared=True,
        next_entry_allowed=True,
    )


def _validate_dependencies(
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    excursion_store: _ExcursionStore,
    session_risk_store: PostgresBybitDemoSessionRiskLedgerStore,
) -> None:
    if evidence_store.live_mainnet_order_routing_allowed:
        raise ValueError("product terminal handoff rejected mainnet-capable evidence store")
    if evidence_store.order_writes_supported or not evidence_store.immutable_records:
        raise ValueError("product terminal handoff requires immutable diagnostics-only evidence")
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("product terminal handoff rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("product terminal handoff requires diagnostics-only excursion store")
    if session_risk_store.live_mainnet_order_routing_allowed:
        raise ValueError("product terminal handoff rejected mainnet-capable session-risk store")
    if session_risk_store.order_writes_supported:
        raise ValueError("product terminal handoff requires diagnostics-only session-risk store")


def _reject_live_result(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"product terminal handoff rejected mainnet-capable {name}")


def _result(
    status: BybitDemoTerminalHandoffStatus,
    *,
    reasons: tuple[str, ...] = (),
    receipt=None,
    acknowledgement=None,
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
