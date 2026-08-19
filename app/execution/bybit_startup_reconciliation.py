from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionCheckpoint
from app.strategy.crypto_perp import CryptoSide

_ZERO = Decimal("0")


class BybitStartupReconciliationStatus(StrEnum):
    READY_FOR_ENTRY = "READY_FOR_ENTRY"
    RESUME_MANAGEMENT = "RESUME_MANAGEMENT"
    TERMINAL_RECOVERY_REQUIRED = "TERMINAL_RECOVERY_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitStartupReconciliationResult:
    status: BybitStartupReconciliationStatus
    reasons: tuple[str, ...]
    checkpoint: BybitDemoExcursionCheckpoint | None
    active_positions: tuple[BybitDemoPosition, ...]
    open_orders: tuple[Mapping[str, Any], ...]
    next_entry_allowed: bool
    management_allowed: bool
    terminal_recovery_required: bool
    broker_truth_complete: bool
    diagnostics_only: bool = True
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False


class BybitStartupBrokerTruth(Protocol):
    live_mainnet_order_routing_allowed: bool

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]: ...

    def get_open_orders(
        self,
        *,
        settle_coin: str = "USDT",
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]: ...


class BybitStartupCheckpointStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def load(self) -> BybitDemoExcursionCheckpoint: ...


def reconcile_bybit_startup(
    *,
    broker: BybitStartupBrokerTruth,
    checkpoint_store: BybitStartupCheckpointStore,
) -> BybitStartupReconciliationResult:
    """Reconcile local active-trade state against broker truth before any new entry.

    This function is intentionally read-only. It never guesses from missing local state and never
    repairs state by issuing broker mutations. A restart with broker exposure but no matching
    checkpoint is blocked so the service cannot duplicate an existing position.
    """

    _reject_live_capability(broker, name="startup broker")
    _validate_checkpoint_store(checkpoint_store)

    try:
        checkpoint = checkpoint_store.load()
    except FileNotFoundError:
        checkpoint = None
    except Exception as exc:  # noqa: BLE001 - corruption/read failure is a hard startup blocker.
        return _result(
            BybitStartupReconciliationStatus.BLOCKED,
            reasons=(f"STARTUP_CHECKPOINT_READ_FAILED:{type(exc).__name__}",),
            checkpoint=None,
            broker_truth_complete=False,
        )

    if checkpoint is not None:
        try:
            checkpoint.validate()
        except Exception as exc:  # noqa: BLE001 - malformed durable state must fail closed.
            return _result(
                BybitStartupReconciliationStatus.BLOCKED,
                reasons=(f"STARTUP_CHECKPOINT_INVALID:{type(exc).__name__}",),
                checkpoint=checkpoint,
                broker_truth_complete=False,
            )
        _reject_live_capability(checkpoint.state, name="startup checkpoint state")

    try:
        positions = tuple(broker.get_positions(settle_coin="USDT"))
        open_orders = tuple(broker.get_open_orders(settle_coin="USDT", limit=50))
    except Exception as exc:  # noqa: BLE001 - incomplete broker truth cannot authorize trading.
        return _result(
            BybitStartupReconciliationStatus.BLOCKED,
            reasons=(f"STARTUP_BROKER_TRUTH_READ_FAILED:{type(exc).__name__}",),
            checkpoint=checkpoint,
            broker_truth_complete=False,
        )

    try:
        active_positions = _validated_active_positions(positions)
        _validate_open_orders(open_orders)
    except Exception as exc:  # noqa: BLE001 - malformed broker truth is not safe to interpret.
        return _result(
            BybitStartupReconciliationStatus.BLOCKED,
            reasons=(f"STARTUP_BROKER_TRUTH_INVALID:{type(exc).__name__}",),
            checkpoint=checkpoint,
            open_orders=open_orders,
            broker_truth_complete=False,
        )

    if checkpoint is None:
        if active_positions:
            return _result(
                BybitStartupReconciliationStatus.BLOCKED,
                reasons=("BROKER_POSITION_WITHOUT_ACTIVE_CHECKPOINT",),
                checkpoint=None,
                active_positions=active_positions,
                open_orders=open_orders,
            )
        if open_orders:
            return _result(
                BybitStartupReconciliationStatus.BLOCKED,
                reasons=("BROKER_OPEN_ORDER_WITHOUT_ACTIVE_CHECKPOINT",),
                checkpoint=None,
                active_positions=active_positions,
                open_orders=open_orders,
            )
        return _result(
            BybitStartupReconciliationStatus.READY_FOR_ENTRY,
            reasons=("BROKER_AND_LOCAL_STATE_FLAT",),
            checkpoint=None,
            active_positions=active_positions,
            open_orders=open_orders,
        )

    if len(active_positions) > 1:
        return _result(
            BybitStartupReconciliationStatus.BLOCKED,
            reasons=("MULTIPLE_BROKER_POSITIONS_WITH_SINGLE_ACTIVE_CHECKPOINT",),
            checkpoint=checkpoint,
            active_positions=active_positions,
            open_orders=open_orders,
        )

    if not active_positions:
        if open_orders:
            return _result(
                BybitStartupReconciliationStatus.BLOCKED,
                reasons=("CHECKPOINT_WITHOUT_POSITION_BUT_OPEN_ORDER_PRESENT",),
                checkpoint=checkpoint,
                active_positions=active_positions,
                open_orders=open_orders,
            )
        return _result(
            BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED,
            reasons=("CHECKPOINT_REMAINS_AFTER_BROKER_POSITION_CLOSED",),
            checkpoint=checkpoint,
            active_positions=active_positions,
            open_orders=open_orders,
        )

    position = active_positions[0]
    mismatch = _position_checkpoint_mismatch(position, checkpoint)
    if mismatch is not None:
        return _result(
            BybitStartupReconciliationStatus.BLOCKED,
            reasons=(mismatch,),
            checkpoint=checkpoint,
            active_positions=active_positions,
            open_orders=open_orders,
        )

    foreign_astra_order = _foreign_astra_open_order(open_orders, checkpoint.entry_order_link_id)
    if foreign_astra_order is not None:
        return _result(
            BybitStartupReconciliationStatus.BLOCKED,
            reasons=("FOREIGN_ASTRA_OPEN_ORDER_DURING_ACTIVE_TRADE",),
            checkpoint=checkpoint,
            active_positions=active_positions,
            open_orders=open_orders,
        )

    return _result(
        BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        reasons=("BROKER_POSITION_MATCHES_ACTIVE_CHECKPOINT",),
        checkpoint=checkpoint,
        active_positions=active_positions,
        open_orders=open_orders,
    )


def _validated_active_positions(
    positions: tuple[BybitDemoPosition, ...],
) -> tuple[BybitDemoPosition, ...]:
    active: list[BybitDemoPosition] = []
    for position in positions:
        if not isinstance(position, BybitDemoPosition):
            raise ValueError("position truth must use BybitDemoPosition")
        if position.symbol != position.symbol.strip().upper() or not position.symbol.endswith("USDT"):
            raise ValueError("position symbol is not normalized USDT")
        if position.side not in {"Buy", "Sell", ""}:
            raise ValueError("position side is invalid")
        if not position.size.is_finite() or position.size < _ZERO:
            raise ValueError("position size must be finite and non-negative")
        if position.size == _ZERO:
            continue
        if position.side not in {"Buy", "Sell"}:
            raise ValueError("active position must have Buy/Sell side")
        active.append(position)
    return tuple(active)


def _validate_open_orders(open_orders: tuple[Mapping[str, Any], ...]) -> None:
    for order in open_orders:
        if not isinstance(order, Mapping):
            raise ValueError("open-order truth row must be an object")
        symbol = order.get("symbol")
        status = order.get("orderStatus")
        if not isinstance(symbol, str) or symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
            raise ValueError("open order symbol is invalid")
        if not isinstance(status, str) or not status:
            raise ValueError("open order status is missing")


def _position_checkpoint_mismatch(
    position: BybitDemoPosition,
    checkpoint: BybitDemoExcursionCheckpoint,
) -> str | None:
    state = checkpoint.state
    expected_side = "Buy" if state.side is CryptoSide.LONG else "Sell"
    if position.symbol != state.symbol:
        return "BROKER_POSITION_SYMBOL_MISMATCH"
    if position.side != expected_side:
        return "BROKER_POSITION_SIDE_MISMATCH"
    if position.size > state.quantity:
        return "BROKER_POSITION_SIZE_EXCEEDS_CHECKPOINT"
    return None


def _foreign_astra_open_order(
    open_orders: tuple[Mapping[str, Any], ...],
    entry_order_link_id: str,
) -> Mapping[str, Any] | None:
    for order in open_orders:
        order_link_id = order.get("orderLinkId")
        if not isinstance(order_link_id, str) or not order_link_id:
            continue
        if order_link_id.startswith("ASTRA-DEMO-") and order_link_id != entry_order_link_id:
            return order
    return None


def _validate_checkpoint_store(store: BybitStartupCheckpointStore) -> None:
    _reject_live_capability(store, name="startup checkpoint store")
    if store.order_writes_supported:
        raise ValueError("startup reconciliation requires a diagnostics-only checkpoint store")


def _reject_live_capability(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"startup reconciliation rejected mainnet-capable {name}")


def _result(
    status: BybitStartupReconciliationStatus,
    *,
    reasons: tuple[str, ...],
    checkpoint: BybitDemoExcursionCheckpoint | None,
    active_positions: tuple[BybitDemoPosition, ...] = (),
    open_orders: tuple[Mapping[str, Any], ...] = (),
    broker_truth_complete: bool = True,
) -> BybitStartupReconciliationResult:
    return BybitStartupReconciliationResult(
        status=status,
        reasons=reasons,
        checkpoint=checkpoint,
        active_positions=active_positions,
        open_orders=open_orders,
        next_entry_allowed=status is BybitStartupReconciliationStatus.READY_FOR_ENTRY,
        management_allowed=status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        terminal_recovery_required=(
            status is BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED
        ),
        broker_truth_complete=broker_truth_complete,
    )
