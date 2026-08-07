from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from app.domain.trading import OrderIntent
from app.oms.store import DurableOmsStore, OrderRecord, OrderState
from app.risk.pretrade import RiskDecision


@dataclass(frozen=True)
class PreparedPaperOrder:
    record: OrderRecord
    client_order_id: str


class PaperOrderLifecycle:
    """Application boundary from approved intent to durable submit outbox.

    This component persists intent/risk/outbox state only. It intentionally does not
    call a broker, so a crash cannot create an unjournaled external mutation.
    """

    def __init__(self, store: DurableOmsStore, *, namespace: str = "astra-paper") -> None:
        if not namespace.strip():
            raise ValueError("namespace is required")
        self.store = store
        self.namespace = namespace.strip().lower()

    def client_order_id(self, intent: OrderIntent) -> str:
        intent.validate()
        digest = hashlib.sha256(
            f"{self.namespace}|{intent.intent_id}".encode("utf-8")
        ).hexdigest()[:32]
        return f"{self.namespace}-{digest}"

    def prepare(
        self,
        intent: OrderIntent,
        decision: RiskDecision,
        *,
        occurred_at: datetime,
    ) -> PreparedPaperOrder:
        intent.validate()
        if not decision.approved:
            raise ValueError("RISK_NOT_APPROVED")
        client_order_id = self.client_order_id(intent)
        record = self.store.create(
            intent,
            client_order_id=client_order_id,
            occurred_at=occurred_at,
        )
        if record.state is OrderState.CREATED:
            record = self.store.approve_risk(
                intent.intent_id,
                event_id=f"risk:{intent.intent_id}",
                occurred_at=occurred_at,
            )
        if record.state is OrderState.RISK_APPROVED:
            record = self.store.enqueue_submit(
                intent.intent_id,
                event_id=f"outbox:{intent.intent_id}",
                occurred_at=occurred_at,
            )
        if record.state in {
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.MANUAL,
        }:
            raise ValueError(f"ORDER_NOT_PREPARABLE:{record.state.value}")
        return PreparedPaperOrder(record=record, client_order_id=client_order_id)
