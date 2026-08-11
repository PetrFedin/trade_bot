from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.application.paper_order_limit_history import SQLiteConfirmedOrderLimitHistory
from app.application.paper_strategy_scope import (
    PaperStrategyIntent,
    SQLitePaperStrategyIntentRegistry,
)
from app.domain.trading import OrderIntent


class SQLitePaperIntentEvidenceRegistry(SQLitePaperStrategyIntentRegistry):
    """Register strategy ownership and immutable initial order limit before outbox.

    The two stores may share one SQLite file. If a crash happens between the two writes,
    replaying the same approved plan is safe: both registrations are idempotent and the
    missing half is repaired before broker submission retry.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.limit_history = SQLiteConfirmedOrderLimitHistory(path)

    def register(
        self,
        intent: OrderIntent,
        *,
        strategy_id: str | None = None,
        registered_at: datetime | None = None,
    ) -> PaperStrategyIntent:
        ownership = super().register(
            intent,
            strategy_id=strategy_id,
            registered_at=registered_at,
        )
        self.limit_history.record_initial(
            intent,
            effective_at=registered_at,
        )
        return ownership
