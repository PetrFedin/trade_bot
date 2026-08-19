from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.application.paper_order_limit_history import (
    ConfirmedOrderLimitEvent,
    SQLiteConfirmedOrderLimitHistory,
)


class ReplacementMutationEvidence(Protocol):
    mutation_id: str
    intent_id: str
    target_limit_price: Decimal | None


@dataclass(frozen=True)
class ConfirmedReplacementEvidence:
    mutation_id: str
    intent_id: str
    limit_price: Decimal
    confirmed_at: datetime
    broker_order_id: str | None
    event: ConfirmedOrderLimitEvent


class PaperConfirmedReplacementRecorder:
    """Persist a replacement limit only after broker-effective confirmation.

    Callers must invoke this from the success/reconciliation path, never from mutation
    request or start. This keeps the historical execution baseline aligned with prices
    proven to have been effective at the broker.
    """

    def __init__(self, history: SQLiteConfirmedOrderLimitHistory) -> None:
        self.history = history

    def record(
        self,
        mutation: ReplacementMutationEvidence,
        *,
        confirmed_at: datetime,
        broker_order_id: str | None = None,
    ) -> ConfirmedReplacementEvidence:
        target = mutation.target_limit_price
        if target is None:
            raise ValueError("confirmed replacement is missing target_limit_price")
        event = self.history.record_confirmed_replace(
            intent_id=mutation.intent_id,
            mutation_id=mutation.mutation_id,
            limit_price=target,
            confirmed_at=confirmed_at,
            broker_order_id=broker_order_id,
        )
        return ConfirmedReplacementEvidence(
            mutation_id=mutation.mutation_id,
            intent_id=mutation.intent_id,
            limit_price=target,
            confirmed_at=event.effective_at,
            broker_order_id=broker_order_id,
            event=event,
        )
