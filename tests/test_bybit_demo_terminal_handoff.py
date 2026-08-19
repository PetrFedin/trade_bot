from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    BybitDemoExcursionRuntimeStatus,
)
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    BybitDemoManagedTradePollResult,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
)
from app.execution.bybit_demo_terminal_handoff import (
    BybitDemoTerminalHandoffStatus,
    persist_and_acknowledge_bybit_demo_terminal_evidence,
)

_ENTRY = "ASTRA-DEMO-E-HANDOFF"
_REVISION = "a" * 64


@dataclass(frozen=True)
class _Checkpoint:
    entry_order_link_id: str = _ENTRY
    revision: str = _REVISION


@dataclass(frozen=True)
class _Lifecycle:
    next_entry_allowed: bool


@dataclass(frozen=True)
class _Accounting:
    lifecycle: _Lifecycle
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Evidence:
    fully_reconciled_all_in: bool = True
    live_mainnet_order_routing_allowed: bool = False


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False


class _EvidenceStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, *, fail: bool = False, idempotent: bool = False) -> None:
        self.fail = fail
        self.idempotent = idempotent
        self.calls = 0

    def persist(self, **_: object) -> BybitDemoTerminalEvidenceReceipt:
        self.calls += 1
        if self.fail:
            raise RuntimeError("disk unavailable")
        return BybitDemoTerminalEvidenceReceipt(
            entry_order_link_id=_ENTRY,
            checkpoint_revision=_REVISION,
            record_sha256="b" * 64,
            idempotent_existing_record=self.idempotent,
        )


def _poll(*, ready: bool = True, lifecycle_allows: bool = True) -> BybitDemoManagedTradePollResult:
    excursion = BybitDemoExcursionRuntimeResult(
        status=BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
        reasons=(),
        checkpoint=_Checkpoint(),
        trade=object(),
        final=object(),
        checkpoint_clear_allowed=True,
    )
    return BybitDemoManagedTradePollResult(
        phase=(
            BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY
            if ready
            else BybitDemoManagedTradePollPhase.TERMINAL_ACCOUNTING_PENDING
        ),
        reasons=(),
        excursion=excursion,
        management=None,
        max_hold_close=None,
        accounting=_Accounting(_Lifecycle(lifecycle_allows)),
        profit_evidence=_Evidence(),
        terminal_evidence_ack_required=True,
        fully_reconciled_all_in=ready,
    )


def _ack_success(**_: object) -> BybitDemoExcursionRuntimeResult:
    return BybitDemoExcursionRuntimeResult(
        status=BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED,
        reasons=(),
        checkpoint=None,
        trade=None,
        final=None,
        checkpoint_clear_allowed=False,
    )


def _ack_blocked(**_: object) -> BybitDemoExcursionRuntimeResult:
    return BybitDemoExcursionRuntimeResult(
        status=BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
        reasons=("EXCURSION_FINAL_ACK_FAILED:RuntimeError",),
        checkpoint=None,
        trade=None,
        final=None,
        checkpoint_clear_allowed=False,
    )


def test_not_ready_poll_never_persists_or_clears_checkpoint() -> None:
    evidence_store = _EvidenceStore()
    ack_called = False

    def _ack(**_: object) -> BybitDemoExcursionRuntimeResult:
        nonlocal ack_called
        ack_called = True
        return _ack_success()

    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _poll(ready=False),
        evidence_store=evidence_store,
        excursion_store=_ExcursionStore(),
        acknowledge=_ack,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.NOT_READY
    assert evidence_store.calls == 0
    assert ack_called is False
    assert result.next_entry_allowed is False


def test_accounting_lifecycle_must_independently_allow_symbol_reuse() -> None:
    evidence_store = _EvidenceStore()
    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _poll(lifecycle_allows=False),
        evidence_store=evidence_store,
        excursion_store=_ExcursionStore(),
        acknowledge=_ack_success,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.NOT_READY
    assert result.reasons == ("ACCOUNTING_LIFECYCLE_DOES_NOT_ALLOW_SYMBOL_REUSE",)
    assert evidence_store.calls == 0


def test_persist_failure_leaves_checkpoint_unacknowledged() -> None:
    evidence_store = _EvidenceStore(fail=True)
    ack_called = False

    def _ack(**_: object) -> BybitDemoExcursionRuntimeResult:
        nonlocal ack_called
        ack_called = True
        return _ack_success()

    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _poll(),
        evidence_store=evidence_store,
        excursion_store=_ExcursionStore(),
        acknowledge=_ack,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.EVIDENCE_PERSIST_FAILED
    assert result.evidence_durable is False
    assert result.checkpoint_cleared is False
    assert ack_called is False
    assert result.next_entry_allowed is False


def test_ack_failure_keeps_durable_evidence_for_retry() -> None:
    evidence_store = _EvidenceStore()
    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _poll(),
        evidence_store=evidence_store,
        excursion_store=_ExcursionStore(),
        acknowledge=_ack_blocked,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED
    assert result.evidence_durable is True
    assert result.checkpoint_cleared is False
    assert result.receipt is not None
    assert result.next_entry_allowed is False


def test_successful_handoff_enables_reentry_only_after_persist_and_exact_ack() -> None:
    evidence_store = _EvidenceStore()
    seen_revision = None

    def _ack(**kwargs: object) -> BybitDemoExcursionRuntimeResult:
        nonlocal seen_revision
        seen_revision = kwargs["expected_revision"]
        return _ack_success()

    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _poll(),
        evidence_store=evidence_store,
        excursion_store=_ExcursionStore(),
        acknowledge=_ack,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert evidence_store.calls == 1
    assert seen_revision == _REVISION
    assert result.evidence_durable is True
    assert result.checkpoint_cleared is True
    assert result.next_entry_allowed is True


def test_idempotent_evidence_record_allows_ack_retry_after_prior_crash() -> None:
    evidence_store = _EvidenceStore(idempotent=True)
    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _poll(),
        evidence_store=evidence_store,
        excursion_store=_ExcursionStore(),
        acknowledge=_ack_success,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert result.receipt is not None
    assert result.receipt.idempotent_existing_record is True
    assert result.next_entry_allowed is True


def test_mainnet_capable_evidence_store_is_rejected() -> None:
    evidence_store = _EvidenceStore()
    evidence_store.live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable evidence store"):
        persist_and_acknowledge_bybit_demo_terminal_evidence(
            _poll(),
            evidence_store=evidence_store,
            excursion_store=_ExcursionStore(),
            acknowledge=_ack_success,
        )
