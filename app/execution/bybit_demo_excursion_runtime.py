from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import (
    BybitDemoExcursionCheckpoint,
    BybitDemoExcursionStore,
)
from app.execution.bybit_demo_excursion_tracker import (
    BybitDemoTradeExcursionFinal,
    finalize_bybit_demo_trade_excursion,
    observe_bybit_demo_trade_excursion,
    start_bybit_demo_trade_excursion,
)
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleResult,
    BybitDemoStrategyCycleStatus,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
    reconcile_bybit_demo_trade,
)


class BybitDemoExcursionRuntimeStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    TRACKING_INITIALIZED = "TRACKING_INITIALIZED"
    OPEN_OBSERVED = "OPEN_OBSERVED"
    TERMINAL_EVIDENCE_READY = "TERMINAL_EVIDENCE_READY"
    TRACKING_BLOCKED = "TRACKING_BLOCKED"
    FINAL_ACKNOWLEDGED = "FINAL_ACKNOWLEDGED"


@dataclass(frozen=True)
class BybitDemoExcursionRuntimeResult:
    status: BybitDemoExcursionRuntimeStatus
    reasons: tuple[str, ...]
    checkpoint: BybitDemoExcursionCheckpoint | None
    trade: BybitDemoTradeMonitorResult | None
    final: BybitDemoTradeExcursionFinal | None
    checkpoint_clear_allowed: bool
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def initialize_bybit_demo_excursion_from_strategy_cycle(
    strategy_cycle: BybitDemoStrategyCycleResult,
    *,
    store: BybitDemoExcursionStore,
) -> BybitDemoExcursionRuntimeResult:
    """Persist MFE/MAE baseline after a genuinely protected demo position exists.

    Tracking is deliberately downstream of entry/protection. Failure to persist diagnostics never
    rewrites the trading decision or pretends the position did not open; it is surfaced explicitly
    so missing excursion evidence cannot later be interpreted as zero giveback.
    """

    _validate_store(store)
    if strategy_cycle.live_mainnet_order_routing_allowed:
        raise ValueError("demo excursion runtime rejected mainnet-capable strategy cycle")
    if strategy_cycle.status is not BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED:
        return _result(BybitDemoExcursionRuntimeStatus.NOT_APPLICABLE)
    orchestrator = strategy_cycle.orchestrator_result
    if orchestrator is None or orchestrator.cycle_result is None:
        return _result(BybitDemoExcursionRuntimeStatus.NOT_APPLICABLE)
    cycle = orchestrator.cycle_result
    if cycle.status.value != "PROTECTED":
        return _result(BybitDemoExcursionRuntimeStatus.NOT_APPLICABLE)
    trade_plan = strategy_cycle.selection.selected_trade_plan
    if trade_plan is None or cycle.entry_ack is None or cycle.reconciled_position is None:
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=("PROTECTED_CYCLE_MISSING_EXCURSION_BASELINE_EVIDENCE",),
        )
    try:
        state = start_bybit_demo_trade_excursion(
            trade_plan,
            position=cycle.reconciled_position,
        )
        checkpoint = store.initialize(
            entry_order_link_id=cycle.entry_ack.order_link_id,
            state=state,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry failure must be explicit, not fatal to trade.
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"EXCURSION_CHECKPOINT_INITIALIZE_FAILED:{type(exc).__name__}",),
        )
    return _result(
        BybitDemoExcursionRuntimeStatus.TRACKING_INITIALIZED,
        checkpoint=checkpoint,
    )


def advance_bybit_demo_excursion_tracking(
    *,
    store: BybitDemoExcursionStore,
    trade_client: Any,
    quote_client: Any,
    execution_limit: int = 100,
) -> BybitDemoExcursionRuntimeResult:
    """Advance persisted excursion state from reconciled fills/position plus a fresh mark quote.

    Terminal evidence is returned without clearing the checkpoint. Callers must persist/report the
    final diagnostic and then call ``acknowledge_bybit_demo_excursion_final`` with the exact current
    revision. This two-phase handoff prevents a crash from erasing MFE/MAE history before the final
    report is durable.
    """

    _validate_store(store)
    _validate_read_client(trade_client, name="trade client")
    _validate_read_client(quote_client, name="quote client")
    try:
        checkpoint = store.load()
    except Exception as exc:  # noqa: BLE001 - unavailable history must never reset to zero.
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"EXCURSION_CHECKPOINT_LOAD_FAILED:{type(exc).__name__}",),
        )

    state = checkpoint.state
    try:
        trade = reconcile_bybit_demo_trade(
            client=trade_client,
            symbol=state.symbol,
            entry_side="Buy" if state.side.value == "LONG" else "Sell",
            entry_order_link_id=checkpoint.entry_order_link_id,
            execution_limit=execution_limit,
        )
    except Exception as exc:  # noqa: BLE001 - unresolved execution evidence keeps checkpoint.
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"EXCURSION_TRADE_RECONCILIATION_FAILED:{type(exc).__name__}",),
            checkpoint=checkpoint,
        )

    if trade.terminal:
        try:
            final = finalize_bybit_demo_trade_excursion(state, trade=trade)
        except Exception as exc:  # noqa: BLE001 - terminal evidence mismatch remains fail-closed.
            return _result(
                BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
                reasons=(f"EXCURSION_FINALIZATION_FAILED:{type(exc).__name__}",),
                checkpoint=checkpoint,
                trade=trade,
            )
        return _result(
            BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
            checkpoint=checkpoint,
            trade=trade,
            final=final,
            checkpoint_clear_allowed=True,
        )

    if trade.status not in {
        BybitDemoTradeMonitorStatus.OPEN,
        BybitDemoTradeMonitorStatus.PARTIALLY_CLOSED,
    }:
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=("EXCURSION_TRADE_STATE_NOT_OBSERVABLE", *trade.reasons),
            checkpoint=checkpoint,
            trade=trade,
        )
    if trade.average_entry_price is None or trade.remaining_quantity <= 0:
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=("EXCURSION_OPEN_TRADE_MISSING_POSITION_BASIS",),
            checkpoint=checkpoint,
            trade=trade,
        )

    try:
        quote = quote_client.get_quote(symbol=state.symbol)
        quote.validate()
        position = BybitDemoPosition(
            symbol=state.symbol,
            side="Buy" if state.side.value == "LONG" else "Sell",
            size=trade.remaining_quantity,
            average_price=trade.average_entry_price,
            unrealised_pnl=None,
            liquidation_price=None,
        )
        updated_state = observe_bybit_demo_trade_excursion(
            state,
            position=position,
            quote=quote,
        )
        updated = store.save(
            entry_order_link_id=checkpoint.entry_order_link_id,
            state=updated_state,
            expected_revision=checkpoint.revision,
        )
    except Exception as exc:  # noqa: BLE001 - observation gaps must be visible and preserved.
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"EXCURSION_OBSERVATION_FAILED:{type(exc).__name__}",),
            checkpoint=checkpoint,
            trade=trade,
        )

    return _result(
        BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED,
        checkpoint=updated,
        trade=trade,
    )


def acknowledge_bybit_demo_excursion_final(
    *,
    store: BybitDemoExcursionStore,
    expected_revision: str,
) -> BybitDemoExcursionRuntimeResult:
    """Clear active state only after the caller has durably consumed terminal evidence."""

    _validate_store(store)
    try:
        store.clear(expected_revision=expected_revision)
    except Exception as exc:  # noqa: BLE001 - stale/missing checkpoint must remain explicit.
        return _result(
            BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"EXCURSION_FINAL_ACK_FAILED:{type(exc).__name__}",),
        )
    return _result(BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED)


def _validate_store(store: BybitDemoExcursionStore) -> None:
    if getattr(store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo excursion runtime rejected mainnet-capable store")
    if getattr(store, "order_writes_supported", True) is not False:
        raise ValueError("demo excursion runtime requires a diagnostics-only store")


def _validate_read_client(client: Any, *, name: str) -> None:
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"demo excursion runtime rejected mainnet-capable {name}")


def _result(
    status: BybitDemoExcursionRuntimeStatus,
    *,
    reasons: tuple[str, ...] = (),
    checkpoint: BybitDemoExcursionCheckpoint | None = None,
    trade: BybitDemoTradeMonitorResult | None = None,
    final: BybitDemoTradeExcursionFinal | None = None,
    checkpoint_clear_allowed: bool = False,
) -> BybitDemoExcursionRuntimeResult:
    return BybitDemoExcursionRuntimeResult(
        status=status,
        reasons=reasons,
        checkpoint=checkpoint,
        trade=trade,
        final=final,
        checkpoint_clear_allowed=checkpoint_clear_allowed,
    )
