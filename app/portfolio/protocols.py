from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.trading import Fill
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.store import PersistedPortfolioSnapshot


class PortfolioStore(Protocol):
    """Persistence port shared by local and PostgreSQL portfolio journals."""

    def append_fill(self, fill: Fill) -> bool: ...

    def append_split(
        self,
        *,
        action_id: str,
        symbol: str,
        ratio: Decimal,
        occurred_at: datetime,
    ) -> bool: ...

    def append_cash_dividend(
        self,
        *,
        action_id: str,
        symbol: str,
        amount_per_share: Decimal,
        occurred_at: datetime,
    ) -> bool: ...

    def replay(self, *, opening_cash: Decimal) -> PortfolioLedger: ...

    def persist_snapshot(
        self,
        ledger: PortfolioLedger,
        *,
        prices: dict[str, Decimal],
        occurred_at: datetime,
    ) -> PersistedPortfolioSnapshot: ...

    def latest_snapshot(self) -> PersistedPortfolioSnapshot | None: ...
