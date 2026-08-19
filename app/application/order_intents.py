from __future__ import annotations

import hashlib
from decimal import Decimal

from app.domain.trading import OrderIntent, Side, TargetPosition


def order_intent_for_target(
    target: TargetPosition,
    *,
    current_quantity: Decimal,
) -> OrderIntent | None:
    """Convert a target position into one deterministic long-only order intent.

    The helper is intentionally strategy-agnostic so single-symbol and portfolio
    planning share the same identity and delta semantics. A target equal to the
    durable current quantity is a no-op.
    """

    target.validate()
    if not current_quantity.is_finite() or current_quantity < 0:
        raise ValueError("current_quantity must be finite and non-negative")

    delta = target.quantity - current_quantity
    if delta == 0:
        return None
    side = Side.BUY if delta > 0 else Side.SELL
    quantity = abs(delta)
    raw_id = (
        f"{target.strategy_id}|{target.symbol}|{target.generated_at.isoformat()}|"
        f"{side.value}|{quantity}|{target.reference_price}"
    )
    return OrderIntent(
        intent_id=hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
        symbol=target.symbol,
        side=side,
        quantity=quantity,
        limit_price=target.reference_price,
        created_at=target.generated_at,
        strategy_id=target.strategy_id,
    )
