from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.domain.trading import Fill, Side
from app.portfolio.ledger import PortfolioLedger, PortfolioSnapshot

UTC = timezone.utc


@dataclass(frozen=True)
class PersistedPortfolioSnapshot:
    snapshot_id: int
    occurred_at: datetime
    payload: dict[str, object]


class PortfolioEventStore:
    """Append-only SQLite portfolio journal capable of deterministic ledger replay."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO portfolio_events
                (event_id, event_type, payload, occurred_at) VALUES (?, ?, ?, ?)""",
                (
                    event_id,
                    event_type,
                    json.dumps(payload, sort_keys=True),
                    moment.isoformat(),
                ),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT event_type, payload, occurred_at FROM portfolio_events ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            payload = dict(json.loads(str(row["payload"])))
            event_type = str(row["event_type"])
            occurred_at = datetime.fromisoformat(str(row["occurred_at"]))
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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO portfolio_snapshots(occurred_at, payload) VALUES (?, ?)",
                (moment.isoformat(), json.dumps(payload, sort_keys=True)),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return PersistedPortfolioSnapshot(snapshot_id, moment, payload)

    def latest_snapshot(self) -> PersistedPortfolioSnapshot | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT snapshot_id, occurred_at, payload
                FROM portfolio_snapshots ORDER BY snapshot_id DESC LIMIT 1"""
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return PersistedPortfolioSnapshot(
            snapshot_id=int(row["snapshot_id"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            payload=dict(json.loads(str(row["payload"]))),
        )
