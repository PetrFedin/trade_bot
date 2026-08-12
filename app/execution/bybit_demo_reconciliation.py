from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo import BybitDemoPosition
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


class BybitDemoReconciliationStatus(StrEnum):
    NO_FILL_EVIDENCE = "NO_FILL_EVIDENCE"
    FILL_EVIDENCE_POSITION_PENDING = "FILL_EVIDENCE_POSITION_PENDING"
    POSITION_CONFIRMED = "POSITION_CONFIRMED"
    POSITION_SIDE_MISMATCH = "POSITION_SIDE_MISMATCH"


@dataclass(frozen=True)
class BybitDemoFillEvidence:
    symbol: str
    side: str
    order_link_id: str
    execution_count: int
    filled_quantity: Decimal
    weighted_average_price: Decimal
    execution_fee: Decimal


@dataclass(frozen=True)
class BybitDemoReconciliation:
    status: BybitDemoReconciliationStatus
    position: BybitDemoPosition | None
    fill: BybitDemoFillEvidence | None
    next_entry_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def aggregate_bybit_demo_executions(
    executions: Sequence[Mapping[str, Any]],
    *,
    expected_symbol: str,
    expected_side: str,
    expected_order_link_id: str,
) -> BybitDemoFillEvidence | None:
    """Aggregate execution rows for one demo order into fill evidence.

    Execution evidence proves that the entry received fills, but it does not replace
    position reconciliation for placing position-level TP/SL.
    """

    if expected_side not in {"Buy", "Sell"}:
        raise ValueError("expected Bybit demo execution side must be Buy or Sell")
    total_qty = Decimal("0")
    total_value = Decimal("0")
    total_fee = Decimal("0")
    count = 0
    for row in executions:
        symbol = row.get("symbol")
        side = row.get("side")
        order_link_id = row.get("orderLinkId")
        if symbol != expected_symbol:
            raise ValueError("Bybit demo execution symbol mismatch")
        if side != expected_side:
            raise ValueError("Bybit demo execution side mismatch")
        if order_link_id not in (None, "", expected_order_link_id):
            raise ValueError("Bybit demo execution orderLinkId mismatch")
        qty = _required_positive_decimal(row, "execQty")
        price = _required_positive_decimal(row, "execPrice")
        fee = _optional_non_negative_decimal(row, "execFee")
        total_qty += qty
        total_value += qty * price
        total_fee += fee
        count += 1
    if count == 0:
        return None
    return BybitDemoFillEvidence(
        symbol=expected_symbol,
        side=expected_side,
        order_link_id=expected_order_link_id,
        execution_count=count,
        filled_quantity=total_qty,
        weighted_average_price=total_value / total_qty,
        execution_fee=total_fee,
    )


def reconcile_bybit_demo_snapshot(
    trade_plan: CryptoTradePlan,
    *,
    positions: Sequence[BybitDemoPosition],
    fill: BybitDemoFillEvidence | None,
) -> BybitDemoReconciliation:
    expected_side = "Buy" if trade_plan.side is CryptoSide.LONG else "Sell"
    same_symbol = [
        position
        for position in positions
        if position.symbol == trade_plan.symbol and position.size > 0
    ]
    for position in same_symbol:
        if position.side != expected_side:
            return BybitDemoReconciliation(
                status=BybitDemoReconciliationStatus.POSITION_SIDE_MISMATCH,
                position=position,
                fill=fill,
            )
    for position in same_symbol:
        if position.average_price is not None:
            return BybitDemoReconciliation(
                status=BybitDemoReconciliationStatus.POSITION_CONFIRMED,
                position=position,
                fill=fill,
            )
    if fill is not None:
        return BybitDemoReconciliation(
            status=BybitDemoReconciliationStatus.FILL_EVIDENCE_POSITION_PENDING,
            position=same_symbol[0] if same_symbol else None,
            fill=fill,
        )
    return BybitDemoReconciliation(
        status=BybitDemoReconciliationStatus.NO_FILL_EVIDENCE,
        position=same_symbol[0] if same_symbol else None,
        fill=None,
    )


def _required_positive_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = _decimal(row, field, required=True)
    if value is None or value <= 0:
        raise ValueError(f"Bybit demo execution {field} must be positive")
    return value


def _optional_non_negative_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = _decimal(row, field, required=False)
    if value is None:
        return Decimal("0")
    if value < 0:
        raise ValueError(f"Bybit demo execution {field} cannot be negative")
    return value


def _decimal(
    row: Mapping[str, Any],
    field: str,
    *,
    required: bool,
) -> Decimal | None:
    raw = row.get(field)
    if raw in (None, ""):
        if required:
            raise ValueError(f"Bybit demo execution missing {field}")
        return None
    try:
        value = Decimal(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo execution has invalid {field}") from exc
    if not value.is_finite():
        raise ValueError(f"Bybit demo execution has non-finite {field}")
    return value
