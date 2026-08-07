from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.trading import Fill, Side
from app.portfolio.ledger import PortfolioLedger, PortfolioSnapshot
from app.portfolio.store import PersistedPortfolioSnapshot

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresPortfolioEventStore:
    """Append-only PostgreSQL portfolio journal with deterministic replay."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use PostgresPortfolioEventStore")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/003_portfolio_events.sql") -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    def _append(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> bool:
        if not event_id.strip():
            raise ValueError("event_id is required")
        moment = self._aware(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_portfolio_events
                        (event_id, event_type, payload, occurred_at)
                        VALUES (%s, %s, %s::jsonb, %s)
                        ON CONFLICT (event_id) DO NOTHING""",
                        (event_id, event_type, json.dumps(payload, sort_keys=True), moment),
                    )
                    return cursor.rowcount == 1

    def append_fill(self, fill: Fill) -> bool:
        fill.validate()
        return self._append(
            event_id=f"fill:{fill.fill_id}",
            event_type="FILL",
            payload={
                "fill_id": fill.fill_id,
                "order_intent_id": fill.order_intent_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
            },
            occurred_at=fill.occurred_at,
        )

    def append_split(
        self,
        *,
        action_id: str,
        symbol: str,
        ratio: Decimal,
        occurred_at: datetime,
    ) -> bool:
        if not ratio.is_finite() or ratio <= 0:
            raise ValueError("split ratio must be positive and finite")
        return self._append(
            event_id=f"corporate-action:{action_id}",
            event_type="SPLIT",
            payload={"action_id": action_id, "symbol": symbol, "ratio": str(ratio)},
            occurred_at=occurred_at,
        )

    def append_cash_dividend(
        self,
        *,
        action_id: str,
        symbol: str,
        amount_per_share: Decimal,
        occurred_at: datetime,
    ) -> bool:
        if not amount_per_share.is_finite() or amount_per_share < 0:
            raise ValueError("amount_per_share must be finite and non-negative")
        return self._append(
            event_id=f"corporate-action:{action_id}",
            event_type="CASH_DIVIDEND",
            payload={
                "action_id": action_id,
                "symbol": symbol,
                "amount_per_share": str(amount_per_share),
            },
            occurred_at=occurred_at,
        )

    def replay(self, *, opening_cash: Decimal) -> PortfolioLedger:
        ledger = PortfolioLedger(opening_cash=opening_cash)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT event_type, payload, occurred_at
                    FROM astra_portfolio_events ORDER BY sequence"""
                )
                rows = cursor.fetchall()
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise RuntimeError("invalid portfolio event payload")
            occurred_at = row["occurred_at"]
            if not isinstance(occurred_at, datetime):
                occurred_at = datetime.fromisoformat(str(occurred_at))
            event_type = str(row["event_type"])
            if event_type == "FILL":
                ledger.apply_fill(
                    Fill(
                        fill_id=str(payload["fill_id"]),
                        order_intent_id=str(payload["order_intent_id"]),
                        symbol=str(payload["symbol"]),
                        side=Side(str(payload["side"])),
                        quantity=Decimal(str(payload["quantity"])),
                        price=Decimal(str(payload["price"])),
                        fee=Decimal(str(payload["fee"])),
                        occurred_at=occurred_at,
                    )
                )
            elif event_type == "SPLIT":
                ledger.apply_split(
                    action_id=str(payload["action_id"]),
                    symbol=str(payload["symbol"]),
                    ratio=Decimal(str(payload["ratio"])),
                )
            elif event_type == "CASH_DIVIDEND":
                ledger.apply_cash_dividend(
                    action_id=str(payload["action_id"]),
                    symbol=str(payload["symbol"]),
                    amount_per_share=Decimal(str(payload["amount_per_share"])),
                )
            else:
                raise RuntimeError(f"unknown portfolio event type: {event_type}")
        return ledger

    @staticmethod
    def _snapshot_payload(snapshot: PortfolioSnapshot) -> dict[str, object]:
        return {
            "cash": str(snapshot.cash),
            "positions": [
                {
                    "symbol": position.symbol,
                    "quantity": str(position.quantity),
                    "average_cost": str(position.average_cost),
                }
                for position in snapshot.positions
            ],
            "gross_notional": str(snapshot.gross_notional),
            "equity": str(snapshot.equity),
            "realized_pnl": str(snapshot.realized_pnl),
            "unrealized_pnl": str(snapshot.unrealized_pnl),
            "cash_income": str(snapshot.cash_income),
            "total_pnl": str(snapshot.total_pnl),
            "fees_paid": str(snapshot.fees_paid),
        }

    def persist_snapshot(
        self,
        ledger: PortfolioLedger,
        *,
        prices: dict[str, Decimal],
        occurred_at: datetime,
    ) -> PersistedPortfolioSnapshot:
        moment = self._aware(occurred_at)
        payload = self._snapshot_payload(ledger.snapshot(prices))
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_portfolio_snapshots(occurred_at, payload)
                        VALUES (%s, %s::jsonb) RETURNING snapshot_id""",
                        (moment, json.dumps(payload, sort_keys=True)),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("portfolio snapshot insert returned no identity")
                    snapshot_id = int(row["snapshot_id"])
        return PersistedPortfolioSnapshot(snapshot_id, moment, payload)

    def latest_snapshot(self) -> PersistedPortfolioSnapshot | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT snapshot_id, occurred_at, payload
                    FROM astra_portfolio_snapshots ORDER BY snapshot_id DESC LIMIT 1"""
                )
                row = cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid portfolio snapshot payload")
        occurred_at = row["occurred_at"]
        if not isinstance(occurred_at, datetime):
            occurred_at = datetime.fromisoformat(str(occurred_at))
        return PersistedPortfolioSnapshot(
            snapshot_id=int(row["snapshot_id"]),
            occurred_at=occurred_at,
            payload=dict(payload),
        )
