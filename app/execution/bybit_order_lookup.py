from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.execution.bybit_demo import BybitDemoHttpJson

SignedGet = Callable[[str, Mapping[str, str]], BybitDemoHttpJson]


@dataclass(frozen=True)
class BybitOrderTruth:
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    quantity: Decimal
    cumulative_executed_quantity: Decimal
    status: str
    reject_reason: str

    @property
    def safely_rejected_without_execution(self) -> bool:
        return self.status == "Rejected" and self.cumulative_executed_quantity == 0

    @property
    def lifecycle_reconciliation_required(self) -> bool:
        return not self.safely_rejected_without_execution


def lookup_bybit_order_by_link_id(
    signed_get: SignedGet,
    *,
    symbol: str,
    order_link_id: str,
    expected_side: str,
    expected_quantity: Decimal,
) -> BybitOrderTruth | None:
    """Read broker order truth without creating, amending or cancelling an order."""

    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit order lookup symbol must be normalized USDT")
    if not order_link_id.strip():
        raise ValueError("Bybit order lookup requires orderLinkId")
    if expected_side not in {"Buy", "Sell"}:
        raise ValueError("Bybit order lookup expected_side must be Buy or Sell")
    if not expected_quantity.is_finite() or expected_quantity <= 0:
        raise ValueError("Bybit order lookup expected_quantity must be positive")

    params = {
        "category": "linear",
        "symbol": symbol,
        "orderLinkId": order_link_id,
        "limit": "1",
    }
    for path in ("/v5/order/realtime", "/v5/order/history"):
        response = signed_get(path, params)
        result = response.payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("Bybit order lookup response missing result")
        rows = result.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("Bybit order lookup response missing list")
        exact = [row for row in rows if row.get("orderLinkId") == order_link_id]
        if len(exact) > 1:
            raise ValueError("Bybit order lookup returned duplicate orderLinkId")
        if exact:
            return _parse_truth(
                exact[0],
                symbol=symbol,
                order_link_id=order_link_id,
                expected_side=expected_side,
                expected_quantity=expected_quantity,
            )
    return None


def _parse_truth(
    row: Mapping[str, object],
    *,
    symbol: str,
    order_link_id: str,
    expected_side: str,
    expected_quantity: Decimal,
) -> BybitOrderTruth:
    order_id = row.get("orderId")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("Bybit recovered order missing orderId")
    if row.get("orderLinkId") != order_link_id:
        raise ValueError("Bybit recovered orderLinkId mismatch")
    if row.get("symbol") != symbol:
        raise ValueError("Bybit recovered symbol mismatch")
    side = row.get("side")
    if side != expected_side:
        raise ValueError("Bybit recovered side mismatch")
    status = row.get("orderStatus")
    if not isinstance(status, str) or not status:
        raise ValueError("Bybit recovered order status missing")
    reject_reason = row.get("rejectReason", "")
    if not isinstance(reject_reason, str):
        raise ValueError("Bybit recovered rejectReason must be string")
    quantity = _decimal(row.get("qty"), name="quantity")
    if quantity != expected_quantity:
        raise ValueError("Bybit recovered order quantity mismatch")
    cumulative = _decimal(row.get("cumExecQty"), name="cumulative executed quantity")
    if cumulative < 0 or cumulative > quantity:
        raise ValueError("Bybit recovered cumulative execution quantity is invalid")
    return BybitOrderTruth(
        order_id=order_id,
        order_link_id=order_link_id,
        symbol=symbol,
        side=expected_side,
        quantity=quantity,
        cumulative_executed_quantity=cumulative,
        status=status,
        reject_reason=reject_reason,
    )


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit recovered order {name} invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"Bybit recovered order {name} must be finite")
    return parsed
