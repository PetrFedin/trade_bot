from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.operational import (
    OperationalBar,
    OperationalBarConflict,
    OperationalDecisionTicket,
    _aware,
    decision_ticket_id,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresOperationalMarketDataStore:
    """PostgreSQL parity for append-only operational market-data truth."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgreSQL market-data store")
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def migrate(self) -> None:
        migration = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "product"
            / "009_operational_marketdata.sql"
        ).read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(migration)

    def record_finalized_for_strategy(
        self,
        bar: OperationalBar,
        *,
        strategy_id: str,
        recorded_at: datetime,
    ) -> OperationalDecisionTicket:
        bar.validate()
        if not bar.is_final:
            raise ValueError("OPERATIONAL_BAR_NOT_FINAL")
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        moment = _aware(recorded_at, "recorded_at")
        if moment < _aware(bar.received_at, "received_at"):
            raise ValueError("recorded_at cannot precede received_at")
        bar_id = bar.bar_id
        content_hash = bar.content_hash
        ticket_id = decision_ticket_id(strategy_id, bar_id)
        conflict = False
        ticket: OperationalDecisionTicket | None = None
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_operational_market_bars(
                            bar_id, provider, venue, symbol, interval_seconds,
                            open_time, close_time, source_timestamp, first_received_at,
                            source_event_id, revision, open_price, high_price, low_price,
                            close_price, volume, content_hash, recorded_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        ) ON CONFLICT (bar_id) DO NOTHING""",
                        self._bar_values(bar, moment),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT content_hash FROM astra_operational_market_bars
                            WHERE bar_id=%s FOR UPDATE""",
                            (bar_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("operational bar idempotency lookup failed")
                        if str(row["content_hash"]) != content_hash:
                            cursor.execute(
                                """INSERT INTO astra_operational_market_bar_conflicts(
                                    bar_id, existing_content_hash, observed_content_hash,
                                    observed_payload, observed_at
                                ) VALUES (%s, %s, %s, %s::jsonb, %s)""",
                                (
                                    bar_id,
                                    str(row["content_hash"]),
                                    content_hash,
                                    json.dumps(
                                        bar.source_payload(),
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                    moment,
                                ),
                            )
                            conflict = True
                    if not conflict:
                        cursor.execute(
                            """INSERT INTO astra_operational_decision_tickets(
                                ticket_id, strategy_id, bar_id, created_at
                            ) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (ticket_id) DO NOTHING""",
                            (ticket_id, strategy_id, bar_id, moment),
                        )
                        cursor.execute(
                            """SELECT ticket_id, strategy_id, bar_id, created_at
                            FROM astra_operational_decision_tickets
                            WHERE ticket_id=%s""",
                            (ticket_id,),
                        )
                        ticket_row = cursor.fetchone()
                        if ticket_row is None:
                            raise RuntimeError("decision ticket persistence failed")
                        ticket = self._ticket(ticket_row)
        if conflict:
            raise OperationalBarConflict(f"OPERATIONAL_BAR_CONFLICT:{bar_id}")
        if ticket is None:
            raise RuntimeError("decision ticket missing after successful persistence")
        return ticket

    @staticmethod
    def _bar_values(bar: OperationalBar, recorded_at: datetime) -> tuple[object, ...]:
        return (
            bar.bar_id,
            bar.provider,
            bar.venue,
            bar.symbol,
            bar.interval_seconds,
            _aware(bar.open_time, "open_time"),
            _aware(bar.close_time, "close_time"),
            _aware(bar.source_timestamp, "source_timestamp"),
            _aware(bar.received_at, "received_at"),
            bar.source_event_id,
            bar.revision,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.content_hash,
            recorded_at,
        )

    def pending_decisions(
        self,
        *,
        strategy_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationalDecisionTicket, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if strategy_id is None:
                    cursor.execute(
                        """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at
                        FROM astra_operational_decision_tickets t
                        LEFT JOIN astra_operational_decision_completions c USING(ticket_id)
                        WHERE c.ticket_id IS NULL
                        ORDER BY t.created_at, t.ticket_id LIMIT %s""",
                        (limit,),
                    )
                else:
                    if not strategy_id.strip():
                        raise ValueError("strategy_id cannot be blank")
                    cursor.execute(
                        """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at
                        FROM astra_operational_decision_tickets t
                        LEFT JOIN astra_operational_decision_completions c USING(ticket_id)
                        WHERE c.ticket_id IS NULL AND t.strategy_id=%s
                        ORDER BY t.created_at, t.ticket_id LIMIT %s""",
                        (strategy_id, limit),
                    )
                rows = cursor.fetchall()
        return tuple(self._ticket(row) for row in rows)

    def recent_bars(
        self,
        *,
        provider: str,
        venue: str,
        symbol: str,
        interval_seconds: int,
        through_close_time: datetime,
        limit: int,
    ) -> tuple[OperationalBar, ...]:
        for name, value in (("provider", provider), ("venue", venue), ("symbol", symbol)):
            if not value or value != value.strip().upper():
                raise ValueError(f"{name} must be non-empty normalized uppercase")
        if interval_seconds < 1 or limit < 1:
            raise ValueError("interval_seconds and limit must be positive")
        through = _aware(through_close_time, "through_close_time")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM astra_operational_market_bars
                    WHERE provider=%s AND venue=%s AND symbol=%s AND interval_seconds=%s
                      AND close_time<=%s
                    ORDER BY close_time DESC, bar_id DESC LIMIT %s""",
                    (provider, venue, symbol, interval_seconds, through, limit),
                )
                rows = cursor.fetchall()
        return tuple(reversed(tuple(self._bar(row) for row in rows)))

    def complete_decision(
        self,
        ticket_id: str,
        *,
        outcome_id: str,
        occurred_at: datetime,
    ) -> bool:
        if not ticket_id.strip() or not outcome_id.strip():
            raise ValueError("ticket_id and outcome_id are required")
        moment = _aware(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT ticket_id FROM astra_operational_decision_tickets
                        WHERE ticket_id=%s""",
                        (ticket_id,),
                    )
                    if cursor.fetchone() is None:
                        raise KeyError(ticket_id)
                    cursor.execute(
                        """INSERT INTO astra_operational_decision_completions(
                            ticket_id, outcome_id, completed_at
                        ) VALUES (%s, %s, %s)
                        ON CONFLICT (ticket_id) DO NOTHING""",
                        (ticket_id, outcome_id, moment),
                    )
                    if cursor.rowcount == 1:
                        return True
                    cursor.execute(
                        """SELECT outcome_id FROM astra_operational_decision_completions
                        WHERE ticket_id=%s""",
                        (ticket_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("decision completion idempotency lookup failed")
                    if str(row["outcome_id"]) != outcome_id:
                        raise ValueError("OPERATIONAL_DECISION_COMPLETION_CONFLICT")
                    return False

    def conflict_count(self) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM astra_operational_market_bar_conflicts"
                )
                row = cursor.fetchone()
        return 0 if row is None else int(row["count"])

    @staticmethod
    def _ticket(row: Mapping[str, object]) -> OperationalDecisionTicket:
        created_at = row["created_at"]
        if not isinstance(created_at, datetime):
            created_at = datetime.fromisoformat(str(created_at))
        ticket = OperationalDecisionTicket(
            ticket_id=str(row["ticket_id"]),
            strategy_id=str(row["strategy_id"]),
            bar_id=str(row["bar_id"]),
            created_at=created_at,
        )
        ticket.validate()
        return ticket

    @staticmethod
    def _bar(row: Mapping[str, object]) -> OperationalBar:
        def moment(name: str) -> datetime:
            value = row[name]
            return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

        bar = OperationalBar(
            provider=str(row["provider"]),
            venue=str(row["venue"]),
            symbol=str(row["symbol"]),
            interval_seconds=int(str(row["interval_seconds"])),
            open_time=moment("open_time"),
            close_time=moment("close_time"),
            source_timestamp=moment("source_timestamp"),
            received_at=moment("first_received_at"),
            source_event_id=str(row["source_event_id"]),
            is_final=True,
            open=Decimal(str(row["open_price"])),
            high=Decimal(str(row["high_price"])),
            low=Decimal(str(row["low_price"])),
            close=Decimal(str(row["close_price"])),
            volume=Decimal(str(row["volume"])),
            revision=int(str(row["revision"])),
        )
        bar.validate()
        return bar
