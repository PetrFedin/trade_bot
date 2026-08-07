from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.portfolio.postgres import PostgresPortfolioEventStore
from app.portfolio.store import PortfolioEventStore


class StrictPortfolioEventStore(PortfolioEventStore):
    """SQLite portfolio store with conflict-aware idempotency."""

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
        canonical_payload = json.dumps(payload, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO portfolio_events
                (event_id, event_type, payload, occurred_at) VALUES (?, ?, ?, ?)""",
                (event_id, event_type, canonical_payload, moment.isoformat()),
            )
            if cursor.rowcount == 1:
                connection.execute("COMMIT")
                return True
            row = connection.execute(
                """SELECT event_type, payload, occurred_at
                FROM portfolio_events WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("portfolio idempotency lookup lost conflicting event")
            identical = (
                str(row["event_type"]) == event_type
                and str(row["payload"]) == canonical_payload
                and str(row["occurred_at"]) == moment.isoformat()
            )
            if not identical:
                raise ValueError("PORTFOLIO_EVENT_CONFLICT")
            connection.execute("COMMIT")
            return False
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()


class StrictPostgresPortfolioEventStore(PostgresPortfolioEventStore):
    """PostgreSQL portfolio store with conflict-aware concurrent idempotency."""

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
        canonical_payload = json.dumps(payload, sort_keys=True)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_portfolio_events
                        (event_id, event_type, payload, occurred_at)
                        VALUES (%s, %s, %s::jsonb, %s)
                        ON CONFLICT (event_id) DO NOTHING""",
                        (event_id, event_type, canonical_payload, moment),
                    )
                    if cursor.rowcount == 1:
                        return True
                    cursor.execute(
                        """SELECT event_type, payload, occurred_at
                        FROM astra_portfolio_events WHERE event_id=%s FOR UPDATE""",
                        (event_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("portfolio idempotency lookup lost conflicting event")
                    existing_payload = row["payload"]
                    if isinstance(existing_payload, str):
                        existing_payload = json.loads(existing_payload)
                    existing_time = row["occurred_at"]
                    if not isinstance(existing_time, datetime):
                        existing_time = datetime.fromisoformat(str(existing_time))
                    identical = (
                        str(row["event_type"]) == event_type
                        and existing_payload == payload
                        and self._aware(existing_time) == moment
                    )
                    if not identical:
                        raise ValueError("PORTFOLIO_EVENT_CONFLICT")
                    return False
