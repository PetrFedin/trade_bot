from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping

from app.oms.store import DurableOmsStore, OrderRecord, OrderState
from app.portfolio.ledger import PortfolioLedger


class BrokerOrderState(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class BrokerOrderTruth:
    client_order_id: str
    broker_order_id: str
    state: BrokerOrderState
    cumulative_filled: Decimal

    def validate(self) -> None:
        if not self.client_order_id.strip() or not self.broker_order_id.strip():
            raise ValueError("broker order identity is required")
        if not self.cumulative_filled.is_finite() or self.cumulative_filled < 0:
            raise ValueError("cumulative_filled must be finite and non-negative")


@dataclass(frozen=True)
class BrokerPositionTruth:
    symbol: str
    quantity: Decimal

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("broker position quantity must be finite and non-negative")


@dataclass(frozen=True)
class BrokerPortfolioTruth:
    cash: Decimal
    positions: tuple[BrokerPositionTruth, ...]

    def validate(self) -> None:
        if not self.cash.is_finite() or self.cash < 0:
            raise ValueError("broker cash must be finite and non-negative")
        seen: set[str] = set()
        for position in self.positions:
            position.validate()
            if position.symbol in seen:
                raise ValueError("duplicate broker position symbol")
            seen.add(position.symbol)


@dataclass(frozen=True)
class PortfolioReconciliationResult:
    matched: bool
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    reasons: tuple[str, ...]


class OmsReconciler:
    """Read-only broker truth reconciler; it never retries or sends broker mutations."""

    def __init__(self, store: DurableOmsStore) -> None:
        self.store = store

    def reconcile_order(
        self,
        intent_id: str,
        broker: BrokerOrderTruth | None,
        *,
        occurred_at: datetime,
        event_prefix: str,
    ) -> OrderRecord:
        local = self.store.get(intent_id)
        if local is None:
            raise KeyError(intent_id)
        if local.terminal and broker is None:
            return local
        if broker is None:
            if local.state is OrderState.UNCERTAIN:
                local = self.store.transition(
                    intent_id,
                    OrderState.RECONCILING,
                    event_id=f"{event_prefix}:begin",
                    occurred_at=occurred_at,
                )
                return self.store.transition(
                    intent_id,
                    OrderState.MANUAL,
                    event_id=f"{event_prefix}:missing",
                    occurred_at=occurred_at,
                    payload={"reason": "BROKER_ORDER_NOT_FOUND"},
                )
            raise ValueError("BROKER_ORDER_NOT_FOUND")

        broker.validate()
        if broker.client_order_id != local.client_order_id:
            raise ValueError("CLIENT_ORDER_ID_MISMATCH")
        if broker.cumulative_filled > local.quantity:
            raise ValueError("BROKER_FILL_EXCEEDS_LOCAL_ORDER")

        if local.state is OrderState.UNCERTAIN:
            local = self.store.transition(
                intent_id,
                OrderState.RECONCILING,
                event_id=f"{event_prefix}:begin",
                occurred_at=occurred_at,
            )
            local = self.store.transition(
                intent_id,
                OrderState.RECONCILED,
                event_id=f"{event_prefix}:truth",
                occurred_at=occurred_at,
                broker_order_id=broker.broker_order_id,
                payload={"broker_state": broker.state.value},
            )

        if broker.cumulative_filled > local.filled_quantity:
            local = self.store.apply_cumulative_fill(
                intent_id,
                event_id=f"{event_prefix}:fill:{broker.cumulative_filled}",
                cumulative_filled=broker.cumulative_filled,
                occurred_at=occurred_at,
                broker_order_id=broker.broker_order_id,
            )

        if broker.state is BrokerOrderState.FILLED:
            if local.filled_quantity != local.quantity:
                raise ValueError("FILLED_STATE_WITH_INCOMPLETE_QUANTITY")
            return local
        if broker.state is BrokerOrderState.PARTIALLY_FILLED:
            if local.filled_quantity <= 0 or local.filled_quantity >= local.quantity:
                raise ValueError("PARTIAL_STATE_WITH_INVALID_QUANTITY")
            return local

        target = {
            BrokerOrderState.OPEN: OrderState.ACKNOWLEDGED,
            BrokerOrderState.CANCELLED: OrderState.CANCELLED,
            BrokerOrderState.REJECTED: OrderState.REJECTED,
        }[broker.state]
        if local.state is target:
            return local
        return self.store.transition(
            intent_id,
            target,
            event_id=f"{event_prefix}:state:{target.value}",
            occurred_at=occurred_at,
            broker_order_id=broker.broker_order_id,
            payload={"broker_state": broker.state.value},
        )


def reconcile_portfolio(
    ledger: PortfolioLedger,
    broker: BrokerPortfolioTruth,
    *,
    cash_tolerance: Decimal = Decimal("0.01"),
    quantity_tolerance: Decimal = Decimal("0"),
) -> PortfolioReconciliationResult:
    broker.validate()
    for name, value in (("cash_tolerance", cash_tolerance), ("quantity_tolerance", quantity_tolerance)):
        if not value.is_finite() or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")

    cash_delta = broker.cash - ledger.cash
    broker_positions: Mapping[str, Decimal] = {position.symbol: position.quantity for position in broker.positions}
    local_positions = {position.symbol: position.quantity for position in ledger.positions()}
    symbols = sorted(set(broker_positions) | set(local_positions))
    deltas: list[tuple[str, Decimal]] = []
    reasons: list[str] = []
    for symbol in symbols:
        delta = broker_positions.get(symbol, Decimal("0")) - local_positions.get(symbol, Decimal("0"))
        if abs(delta) > quantity_tolerance:
            deltas.append((symbol, delta))
            reasons.append(f"POSITION_MISMATCH:{symbol}")
    if abs(cash_delta) > cash_tolerance:
        reasons.append("CASH_MISMATCH")
    return PortfolioReconciliationResult(
        matched=not reasons,
        cash_delta=cash_delta,
        position_deltas=tuple(deltas),
        reasons=tuple(reasons),
    )
