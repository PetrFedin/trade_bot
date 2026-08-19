from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo_entry_provenance_store import BybitDemoEntryProvenanceRecord
from app.execution.bybit_demo_terminal_handoff import BybitDemoTerminalHandoffStatus
from app.execution.bybit_demo_trade_attribution import (
    BybitDemoTradeAttribution,
    build_bybit_demo_trade_attribution,
)
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeResult,
    BybitDemoTradingRuntimeStatus,
    run_bybit_demo_trading_runtime,
)
from app.marketdata.bybit_v5 import BybitKlineBar


class BybitDemoAttributedRuntimeStatus(StrEnum):
    RUNTIME_PASSTHROUGH = "RUNTIME_PASSTHROUGH"
    TERMINAL_ATTRIBUTION_READY = "TERMINAL_ATTRIBUTION_READY"
    TERMINAL_ATTRIBUTION_GAP = "TERMINAL_ATTRIBUTION_GAP"
    TERMINAL_HANDOFF_PROOF_INVALID = "TERMINAL_HANDOFF_PROOF_INVALID"


@dataclass(frozen=True)
class BybitDemoAttributedRuntimeResult:
    status: BybitDemoAttributedRuntimeStatus
    reasons: tuple[str, ...]
    runtime: BybitDemoTradingRuntimeResult
    trade_attribution: BybitDemoTradeAttribution | None
    trade_attribution_built: bool
    next_entry_allowed: bool
    same_invocation_additional_entry_allowed: bool = False
    diagnostics_only: bool = True
    automatic_selector_retuning_allowed: bool = False
    automatic_exit_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


class BybitDemoReadableEntryProvenanceStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    immutable_records: bool
    realized_pnl_storage_allowed: bool

    def load(self, *, entry_order_link_id: str) -> BybitDemoEntryProvenanceRecord: ...


RuntimeRunner = Callable[..., BybitDemoTradingRuntimeResult]
AttributionBuilder = Callable[..., BybitDemoTradeAttribution]


def run_attributed_bybit_demo_trading_runtime(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    entry_provenance_store: BybitDemoReadableEntryProvenanceStore,
    terminal_evidence_store: Any,
    runtime_runner: RuntimeRunner = run_bybit_demo_trading_runtime,
    attribution_builder: AttributionBuilder = build_bybit_demo_trade_attribution,
    **runtime_kwargs: Any,
) -> BybitDemoAttributedRuntimeResult:
    """Run the single-writer demo runtime and reconstruct terminal trade attribution.

    The underlying trading runtime remains authoritative for entries, position management,
    accounting and terminal handoff. This wrapper adds a restart-safe post-trade join: after a
    proven terminal handoff, it reloads the immutable outcome-free entry decision by orderLinkId
    and joins it to the fully reconciled terminal evidence. Analytics failures do not rewrite an
    already completed trading lifecycle, but malformed terminal proof fails closed for re-entry.
    """

    _validate_provenance_store(entry_provenance_store)
    base = runtime_runner(
        bars_by_symbol,
        entry_provenance_store=entry_provenance_store,
        terminal_evidence_store=terminal_evidence_store,
        **runtime_kwargs,
    )
    _reject_live_result(base, name="base trading runtime")
    if base.same_invocation_additional_entry_allowed:
        raise ValueError("attributed runtime rejected same-invocation replacement entry")

    if base.status is not BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE:
        return _result(
            BybitDemoAttributedRuntimeStatus.RUNTIME_PASSTHROUGH,
            runtime=base,
            reasons=base.reasons,
            next_entry_allowed=base.next_entry_allowed,
        )

    invalid_reason = _terminal_proof_invalid_reason(base)
    if invalid_reason is not None:
        return _result(
            BybitDemoAttributedRuntimeStatus.TERMINAL_HANDOFF_PROOF_INVALID,
            runtime=base,
            reasons=(invalid_reason,),
            next_entry_allowed=False,
        )

    handoff = base.terminal_handoff
    managed = base.managed_poll
    if handoff is None or handoff.receipt is None or managed is None:
        raise AssertionError("validated terminal proof became unavailable")
    evidence = managed.profit_evidence
    if evidence is None:
        raise AssertionError("validated terminal evidence became unavailable")

    try:
        loaded = entry_provenance_store.load(
            entry_order_link_id=handoff.receipt.entry_order_link_id
        )
        _reject_live_result(loaded, name="entry provenance record")
        attribution = attribution_builder(
            loaded.provenance,
            evidence,
            terminal_receipt=handoff.receipt,
        )
        _reject_live_result(attribution, name="trade attribution")
    except Exception as exc:  # noqa: BLE001 - immutable evidence allows later analytics retry.
        return _result(
            BybitDemoAttributedRuntimeStatus.TERMINAL_ATTRIBUTION_GAP,
            runtime=base,
            reasons=(f"TERMINAL_TRADE_ATTRIBUTION_FAILED:{type(exc).__name__}",),
            next_entry_allowed=base.next_entry_allowed,
        )

    return _result(
        BybitDemoAttributedRuntimeStatus.TERMINAL_ATTRIBUTION_READY,
        runtime=base,
        trade_attribution=attribution,
        trade_attribution_built=True,
        next_entry_allowed=base.next_entry_allowed,
    )


def _terminal_proof_invalid_reason(base: BybitDemoTradingRuntimeResult) -> str | None:
    handoff = base.terminal_handoff
    managed = base.managed_poll
    if handoff is None:
        return "TERMINAL_HANDOFF_RESULT_MISSING"
    if handoff.status is not BybitDemoTerminalHandoffStatus.COMPLETE:
        return "TERMINAL_HANDOFF_STATUS_NOT_COMPLETE"
    if not handoff.evidence_durable:
        return "TERMINAL_EVIDENCE_NOT_DURABLE"
    if not handoff.checkpoint_cleared:
        return "TERMINAL_EXCURSION_CHECKPOINT_NOT_CLEARED"
    if not handoff.next_entry_allowed:
        return "TERMINAL_HANDOFF_DID_NOT_ALLOW_REENTRY"
    if handoff.receipt is None:
        return "TERMINAL_EVIDENCE_RECEIPT_MISSING"
    if managed is None:
        return "TERMINAL_MANAGED_POLL_MISSING"
    if not managed.fully_reconciled_all_in:
        return "TERMINAL_MANAGED_POLL_NOT_FULLY_RECONCILED"
    if managed.profit_evidence is None:
        return "TERMINAL_PROFIT_EVIDENCE_MISSING"
    if not managed.profit_evidence.fully_reconciled_all_in:
        return "TERMINAL_PROFIT_EVIDENCE_NOT_FULLY_RECONCILED"
    if not base.next_entry_allowed:
        return "BASE_RUNTIME_DID_NOT_ALLOW_NEXT_ENTRY"
    return None


def _validate_provenance_store(store: BybitDemoReadableEntryProvenanceStore) -> None:
    if store.live_mainnet_order_routing_allowed:
        raise ValueError("attributed runtime rejected mainnet-capable provenance store")
    if store.order_writes_supported:
        raise ValueError("attributed runtime requires diagnostics-only provenance store")
    if not store.immutable_records:
        raise ValueError("attributed runtime requires immutable provenance records")
    if store.realized_pnl_storage_allowed:
        raise ValueError("attributed runtime forbids realized PnL in entry provenance")


def _reject_live_result(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"attributed runtime rejected mainnet-capable {name}")


def _result(
    status: BybitDemoAttributedRuntimeStatus,
    *,
    runtime: BybitDemoTradingRuntimeResult,
    reasons: tuple[str, ...] = (),
    trade_attribution: BybitDemoTradeAttribution | None = None,
    trade_attribution_built: bool = False,
    next_entry_allowed: bool,
) -> BybitDemoAttributedRuntimeResult:
    return BybitDemoAttributedRuntimeResult(
        status=status,
        reasons=reasons,
        runtime=runtime,
        trade_attribution=trade_attribution,
        trade_attribution_built=trade_attribution_built,
        next_entry_allowed=next_entry_allowed,
        same_invocation_additional_entry_allowed=False,
    )
