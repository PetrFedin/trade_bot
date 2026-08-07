from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    close: Decimal

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if not self.close.is_finite() or self.close <= 0:
            raise ValueError("close must be positive and finite")


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    quantity: Decimal
    reference_price: Decimal
    generated_at: datetime
    strategy_id: str

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("target quantity must be finite and non-negative")
        if not self.reference_price.is_finite() or self.reference_price <= 0:
            raise ValueError("reference price must be positive and finite")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    created_at: datetime
    strategy_id: str

    def validate(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id is required")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("quantity must be positive and finite")
        if not self.limit_price.is_finite() or self.limit_price <= 0:
            raise ValueError("limit_price must be positive and finite")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_intent_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    fee: Decimal = Decimal("0")

    def validate(self) -> None:
        if not self.fill_id.strip() or not self.order_intent_id.strip():
            raise ValueError("fill identity is required")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("fill quantity must be positive and finite")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("fill price must be positive and finite")
        if not self.fee.is_finite() or self.fee < 0:
            raise ValueError("fill fee must be finite and non-negative")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
