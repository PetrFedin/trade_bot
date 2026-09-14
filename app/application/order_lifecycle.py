from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from app.domain.trading import OrderIntent
from app.oms.protocols import OmsStore
from app.oms.risk_reservations import RiskReservationBudget
from app.oms.store import OrderRecord, OrderState
from app.risk.pretrade import RiskDecision, risk_intent_fingerprint


@dataclass(frozen=True)
class PreparedPaperOrder:
    record: OrderRecord
    client_order_id: str


class PaperOrderLifecycle:
    """Application boundary from an exact risk-approved intent to durable outbox.

    This component persists intent/risk/outbox state only. It intentionally does not
    call a broker, so a crash cannot create an unjournaled external mutation. A risk
    decision is accepted only when it is bound to this exact intent identity and
    economics; an unrelated or manually fabricated unbound approval cannot advance
    the order to RISK_APPROVED.
    """

    def __init__(self, store: OmsStore, *, namespace: str = "astra-paper") -> None:
        if not namespace.strip():
            raise ValueError("namespace is required")
        self.store = store
        self.namespace = namespace.strip().lower()

    def client_order_id(self, intent: OrderIntent) -> str:
        intent.validate()
        digest = hashlib.sha256(f"{self.namespace}|{intent.intent_id}".encode()).hexdigest()[:32]
        return f"{self.namespace}-{digest}"

    @staticmethod
    def _validate_risk_approval(intent: OrderIntent, decision: RiskDecision) -> None:
        if not decision.approved:
            raise ValueError("RISK_NOT_APPROVED")
        if not decision.intent_id.strip() or not decision.intent_fingerprint.strip():
            raise ValueError("RISK_APPROVAL_NOT_BOUND")
        if decision.intent_id != intent.intent_id:
            raise ValueError("RISK_APPROVAL_INTENT_MISMATCH")
        expected_notional = intent.quantity * intent.limit_price
        if decision.order_notional != expected_notional:
            raise ValueError("RISK_APPROVAL_ECONOMICS_MISMATCH")
        if decision.intent_fingerprint != risk_intent_fingerprint(intent):
            raise ValueError("RISK_APPROVAL_INTENT_MISMATCH")

    def prepare(
        self,
        intent: OrderIntent,
        decision: RiskDecision,
        *,
        occurred_at: datetime,
        reservation_budget: RiskReservationBudget | None = None,
    ) -> PreparedPaperOrder:
        intent.validate()
        self._validate_risk_approval(intent, decision)
        client_order_id = self.client_order_id(intent)
        record = self.store.create(
            intent,
            client_order_id=client_order_id,
            occurred_at=occurred_at,
        )
        if record.state is OrderState.CREATED:
            if reservation_budget is None:
                record = self.store.approve_risk(
                    intent.intent_id,
                    event_id=f"risk:{intent.intent_id}",
                    occurred_at=occurred_at,
                )
            else:
                reserve = getattr(self.store, "approve_risk_with_reservation", None)
                if reserve is None:
                    raise RuntimeError("OMS_RISK_RESERVATION_UNSUPPORTED")
                record = reserve(
                    intent.intent_id,
                    event_id=f"risk:{intent.intent_id}",
                    occurred_at=occurred_at,
                    budget=reservation_budget,
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
