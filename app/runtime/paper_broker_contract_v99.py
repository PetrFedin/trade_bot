from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, Sequence

from app.runtime.platform_common_v90 import require_aware


class BrokerMutationError(RuntimeError):
    def __init__(self, code: str, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerOrderStatus(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REPLACED = "REPLACED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"


@dataclass(frozen=True)
class BrokerAccount:
    account_id: str
    status: str
    currency: str
    buying_power: Decimal
    trading_blocked: bool = False

    def validate(self) -> None:
        if not self.account_id.strip():
            raise ValueError("account_id is required")
        if not self.status.strip() or not self.currency.strip():
            raise ValueError("status and currency are required")
        if not self.buying_power.is_finite():
            raise ValueError("buying_power must be finite")


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    broker_order_id: str
    instrument: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    status: BrokerOrderStatus
    filled_quantity: Decimal
    updated_at: datetime
    filled_avg_price: Decimal | None = None

    def validate(self) -> None:
        require_aware(self.updated_at, field_name="updated_at")
        for name, value in (
            ("client_order_id", self.client_order_id),
            ("broker_order_id", self.broker_order_id),
            ("instrument", self.instrument),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.instrument != self.instrument.upper():
            raise ValueError("instrument must be uppercase")
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise ValueError("quantity must be positive and finite")
        if self.limit_price <= 0 or not self.limit_price.is_finite():
            raise ValueError("limit_price must be positive and finite")
        if (
            not self.filled_quantity.is_finite()
            or self.filled_quantity < 0
            or self.filled_quantity > self.quantity
        ):
            raise ValueError("filled_quantity is outside order quantity")
        if self.filled_avg_price is not None and (
            not self.filled_avg_price.is_finite() or self.filled_avg_price <= 0
        ):
            raise ValueError("filled_avg_price must be positive and finite when present")


class PaperBrokerV99(Protocol):
    paper_order_writes_enabled: bool

    def get_account(self) -> BrokerAccount: ...

    def list_open_orders(self) -> Sequence[BrokerOrder]: ...

    def submit_limit_order(
        self,
        *,
        client_order_id: str,
        instrument: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> BrokerOrder: ...

    def replace_limit_order(
        self, *, broker_order_id: str, limit_price: Decimal
    ) -> BrokerOrder: ...

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder: ...

    def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None: ...
