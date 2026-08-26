from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from app.execution.bybit_demo_attributed_runtime import (
    BybitDemoAttributedRuntimeStatus,
    run_attributed_bybit_demo_trading_runtime,
)
from app.execution.bybit_demo_entry_provenance_store import BybitDemoEntryProvenanceRecord
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPhase
from app.execution.bybit_demo_session_risk_runtime import BybitDemoSessionRiskCommitReceipt
from app.execution.bybit_demo_terminal_evidence_store import BybitDemoTerminalEvidenceReceipt
from app.execution.bybit_demo_terminal_handoff import (
    BybitDemoTerminalHandoffResult,
    BybitDemoTerminalHandoffStatus,
)
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeResult,
    BybitDemoTradingRuntimeStatus,
)

_ENTRY = "ASTRA-DEMO-E-ATTRIBUTED"


@dataclass(frozen=True)
class _Evidence:
    fully_reconciled_all_in: bool = True
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Managed:
    phase: BybitDemoManagedTradePollPhase
    fully_reconciled_all_in: bool
    profit_evidence: _Evidence | None
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Attribution:
    live_mainnet_order_routing_allowed: bool = False


class _ProvenanceStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True
    realized_pnl_storage_allowed = False

    def __init__(
        self,
        *,
        provenance: object | None = object(),
        load_error: Exception | None = None,
        unsafe_record: bool = False,
    ) -> None:
        self.provenance = provenance
        self.load_error = load_error
        self.unsafe_record = unsafe_record
        self.loaded_ids: list[str] = []

    def load(self, *, entry_order_link_id: str) -> BybitDemoEntryProvenanceRecord:
        self.loaded_ids.append(entry_order_link_id)
        if self.load_error is not None:
            raise self.load_error
        return BybitDemoEntryProvenanceRecord(
            provenance=self.provenance,  # type: ignore[arg-type]
            record_sha256="c" * 64,
            live_mainnet_order_routing_allowed=self.unsafe_record,
        )


class _TerminalStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False


def _receipt() -> BybitDemoTerminalEvidenceReceipt:
    return BybitDemoTerminalEvidenceReceipt(
        entry_order_link_id=_ENTRY,
        checkpoint_revision="a" * 64,
        record_sha256="b" * 64,
        idempotent_existing_record=False,
    )


def _risk_receipt(*, entry_order_link_id: str = _ENTRY) -> BybitDemoSessionRiskCommitReceipt:
    return BybitDemoSessionRiskCommitReceipt(
        ledger_revision_sha256="d" * 64,
        outcome_count=1,
        entry_order_link_id=entry_order_link_id,
        idempotent_existing_outcome=False,
    )


def _handoff(
    *,
    evidence_durable: bool = True,
    session_risk_durable: bool = True,
    session_risk_receipt: BybitDemoSessionRiskCommitReceipt | None = None,
    checkpoint_cleared: bool = True,
    next_entry_allowed: bool = True,
) -> BybitDemoTerminalHandoffResult:
    if session_risk_receipt is None and session_risk_durable:
        session_risk_receipt = _risk_receipt()
    return BybitDemoTerminalHandoffResult(
        status=BybitDemoTerminalHandoffStatus.COMPLETE,
        reasons=(),
        receipt=_receipt(),
        session_risk_receipt=session_risk_receipt,
        acknowledgement=None,
        evidence_durable=evidence_durable,
        session_risk_durable=session_risk_durable,
        checkpoint_cleared=checkpoint_cleared,
        next_entry_allowed=next_entry_allowed,
    )


def _base(
    *,
    status: BybitDemoTradingRuntimeStatus = BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE,
    handoff: BybitDemoTerminalHandoffResult | None = None,
    managed: _Managed | None = None,
    next_entry_allowed: bool = True,
) -> BybitDemoTradingRuntimeResult:
    if handoff is None and status is BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE:
        handoff = _handoff()
    if managed is None and status is BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE:
        managed = _Managed(
            phase=BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY,
            fully_reconciled_all_in=True,
            profit_evidence=_Evidence(),
        )
    return BybitDemoTradingRuntimeResult(
        status=status,
        reasons=(),
        entry_result=None,
        managed_poll=managed,  # type: ignore[arg-type]
        terminal_handoff=handoff,
        runtime_lease_acquired=True,
        runtime_lease_released=True,
        next_entry_allowed=next_entry_allowed,
    )


def _run(
    base: BybitDemoTradingRuntimeResult,
    *,
    store: _ProvenanceStore | None = None,
    builder=None,
):
    active_store = _ProvenanceStore() if store is None else store
    seen_kwargs: dict[str, object] = {}

    def _runtime(_bars, **kwargs):
        seen_kwargs.update(kwargs)
        return base

    active_builder = (lambda *_args, **_kwargs: _Attribution()) if builder is None else builder
    result = run_attributed_bybit_demo_trading_runtime(
        {},
        entry_provenance_store=active_store,
        terminal_evidence_store=_TerminalStore(),
        runtime_runner=_runtime,
        attribution_builder=active_builder,
    )
    return result, active_store, seen_kwargs


def test_nonterminal_runtime_passes_through_without_loading_provenance() -> None:
    base = _base(
        status=BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED,
        handoff=None,
        managed=None,
        next_entry_allowed=False,
    )
    result, store, seen_kwargs = _run(base)
    assert result.status is BybitDemoAttributedRuntimeStatus.RUNTIME_PASSTHROUGH
    assert result.runtime is base
    assert result.trade_attribution is None
    assert result.next_entry_allowed is False
    assert store.loaded_ids == []
    assert seen_kwargs["entry_provenance_store"] is store
    assert isinstance(seen_kwargs["terminal_evidence_store"], _TerminalStore)


def test_terminal_handoff_loads_exact_entry_and_builds_attribution() -> None:
    provenance = object()
    store = _ProvenanceStore(provenance=provenance)
    seen: dict[str, object] = {}

    def _builder(entry, evidence, *, terminal_receipt):
        seen["entry"] = entry
        seen["evidence"] = evidence
        seen["receipt"] = terminal_receipt
        return _Attribution()

    base = _base()
    result, _, _ = _run(base, store=store, builder=_builder)
    assert result.status is BybitDemoAttributedRuntimeStatus.TERMINAL_ATTRIBUTION_READY
    assert result.trade_attribution_built is True
    assert isinstance(result.trade_attribution, _Attribution)
    assert store.loaded_ids == [_ENTRY]
    assert seen["entry"] is provenance
    assert seen["evidence"] is base.managed_poll.profit_evidence
    assert seen["receipt"] is base.terminal_handoff.receipt
    assert result.next_entry_allowed is True
    assert result.same_invocation_additional_entry_allowed is False
    assert result.automatic_selector_retuning_allowed is False
    assert result.automatic_exit_retuning_allowed is False


def test_missing_provenance_is_retryable_analytics_gap_after_completed_lifecycle() -> None:
    store = _ProvenanceStore(load_error=FileNotFoundError())
    builder_called = False

    def _builder(*_args, **_kwargs):
        nonlocal builder_called
        builder_called = True
        return _Attribution()

    result, _, _ = _run(_base(), store=store, builder=_builder)
    assert result.status is BybitDemoAttributedRuntimeStatus.TERMINAL_ATTRIBUTION_GAP
    assert result.reasons == ("TERMINAL_PROVENANCE_LOAD_FAILED:FileNotFoundError",)
    assert result.trade_attribution is None
    assert result.next_entry_allowed is True
    assert builder_called is False


def test_attribution_build_failure_is_retryable_gap_with_immutable_inputs() -> None:
    def _fail(*_args, **_kwargs):
        raise RuntimeError("analytics unavailable")

    result, _, _ = _run(_base(), builder=_fail)
    assert result.status is BybitDemoAttributedRuntimeStatus.TERMINAL_ATTRIBUTION_GAP
    assert result.reasons == ("TERMINAL_TRADE_ATTRIBUTION_BUILD_FAILED:RuntimeError",)
    assert result.next_entry_allowed is True
    assert result.trade_attribution_built is False


def test_invalid_terminal_evidence_proof_fails_closed_for_reentry() -> None:
    result, store, _ = _run(_base(handoff=_handoff(evidence_durable=False)))
    assert result.status is BybitDemoAttributedRuntimeStatus.TERMINAL_HANDOFF_PROOF_INVALID
    assert result.reasons == ("TERMINAL_EVIDENCE_NOT_DURABLE",)
    assert result.next_entry_allowed is False
    assert store.loaded_ids == []


def test_invalid_terminal_session_risk_proof_fails_closed_for_reentry() -> None:
    result, store, _ = _run(
        _base(
            handoff=_handoff(
                session_risk_durable=False,
                session_risk_receipt=None,
            )
        )
    )
    assert result.status is BybitDemoAttributedRuntimeStatus.TERMINAL_HANDOFF_PROOF_INVALID
    assert result.reasons == ("TERMINAL_SESSION_RISK_NOT_DURABLE",)
    assert result.next_entry_allowed is False
    assert store.loaded_ids == []


def test_mismatched_terminal_session_risk_identity_fails_closed() -> None:
    result, store, _ = _run(
        _base(
            handoff=_handoff(
                session_risk_receipt=_risk_receipt(
                    entry_order_link_id="ASTRA-DEMO-E-OTHER"
                )
            )
        )
    )
    assert result.status is BybitDemoAttributedRuntimeStatus.TERMINAL_HANDOFF_PROOF_INVALID
    assert result.reasons == ("TERMINAL_SESSION_RISK_ENTRY_ID_MISMATCH",)
    assert result.next_entry_allowed is False
    assert store.loaded_ids == []


def test_unsafe_loaded_provenance_record_is_hard_rejected() -> None:
    store = _ProvenanceStore(unsafe_record=True)
    with pytest.raises(ValueError, match="mainnet-capable entry provenance record"):
        _run(_base(), store=store)


def test_unsafe_attribution_result_is_hard_rejected() -> None:
    with pytest.raises(ValueError, match="mainnet-capable trade attribution"):
        _run(
            _base(),
            builder=lambda *_args, **_kwargs: _Attribution(
                live_mainnet_order_routing_allowed=True
            ),
        )


def test_same_invocation_replacement_permission_is_rejected() -> None:
    base = replace(_base(), same_invocation_additional_entry_allowed=True)
    with pytest.raises(ValueError, match="same-invocation replacement entry"):
        _run(base)


def test_unsafe_provenance_store_is_rejected_before_base_runtime() -> None:
    store = _ProvenanceStore()
    store.realized_pnl_storage_allowed = True
    with pytest.raises(ValueError, match="forbids realized PnL"):
        _run(_base(), store=store)
