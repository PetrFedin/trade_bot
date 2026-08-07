from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.trading import OrderIntent
from app.oms.store import OrderRecord, OrderState, OutboxMessage


class OmsStore(Protocol):
    """Persistence port shared by SQLite, PostgreSQL and execution services."""

    def get(self, intent_id: str) -> OrderRecord | None: ...

    def create(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        occurred_at: datetime | None = None,
    ) -> OrderRecord: ...

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        event_id: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> OrderRecord: ...

    def approve_risk(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
    ) -> OrderRecord: ...

    def enqueue_submit(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
    ) -> OrderRecord: ...

    def pending_outbox(self, *, limit: int = 100) -> tuple[OutboxMessage, ...]: ...

    def mark_outbox_published(self, message_id: int, *, occurred_at: datetime) -> None: ...

    def apply_cumulative_fill(
        self,
        intent_id: str,
        *,
        event_id: str,
        cumulative_filled: Decimal,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        cumulative_notional: Decimal | None = None,
        cumulative_fee: Decimal | None = None,
    ) -> OrderRecord: ...

    def events(self, intent_id: str) -> tuple[Mapping[str, object], ...]: ...
