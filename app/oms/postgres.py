from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.trading import OrderIntent, Side
from app.oms.store import DurableOmsStore, OrderRecord, OrderState, OutboxMessage

UTC = UTC

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised by optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresOmsStore:
    """PostgreSQL OMS backend using row locks for multi-worker serialization."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use PostgresOmsStore")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/001_durable_oms.sql") -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _now(value: datetime | None = None) -> datetime:
        moment = datetime.now(UTC) if value is None else value
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return moment.astimezone(UTC)

    @staticmethod
    def _row(row: dict[str, object]) -> OrderRecord:
        updated_at = row["updated_at"]
        if not isinstance(updated_at, datetime):
            updated_at = datetime.fromisoformat(str(updated_at))
        return OrderRecord(
            intent_id=str(row["intent_id"]),
            client_order_id=str(row["client_order_id"]),
            broker_order_id=str(row["broker_order_id"]),
            symbol=str(row["symbol"]),
            side=Side(str(row["side"])),
            quantity=Decimal(str(row["quantity"])),
            limit_price=Decimal(str(row["limit_price"])),
            filled_quantity=Decimal(str(row["filled_quantity"])),
            state=OrderState(str(row["state"])),
            version=int(row["version"]),
            updated_at=updated_at,
        )

    @staticmethod
    def _load_for_update(cursor, intent_id: str) -> OrderRecord:
        cursor.execute("SELECT * FROM astra_oms_orders WHERE intent_id=%s FOR UPDATE", (intent_id,))
        row = cursor.fetchone()
        if row is None:
            raise KeyError(intent_id)
        return PostgresOmsStore._row(row)

    @staticmethod
    def _event_exists(cursor, event_id: str) -> bool:
        cursor.execute("SELECT 1 FROM astra_oms_events WHERE event_id=%s", (event_id,))
        return cursor.fetchone() is not None

    @staticmethod
    def _append_event(
        cursor,
        *,
        event_id: str,
        intent_id: str,
        event_type: str,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> bool:
        cursor.execute(
            """INSERT INTO astra_oms_events(event_id, intent_id, event_type, payload, occurred_at)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING""",
            (event_id, intent_id, event_type, json.dumps(payload, sort_keys=True), occurred_at),
        )
        return cursor.rowcount == 1

    def get(self, intent_id: str) -> OrderRecord | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM astra_oms_orders WHERE intent_id=%s", (intent_id,))
                row = cursor.fetchone()
                return None if row is None else self._row(row)

    def create(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        occurred_at: datetime | None = None,
    ) -> OrderRecord:
        intent.validate()
        if not client_order_id.strip():
            raise ValueError("client_order_id is required")
        moment = self._now(occurred_at or intent.created_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_oms_orders
                        (intent_id, client_order_id, symbol, side, quantity, limit_price,
                         filled_quantity, state, version, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, 0, %s, 1, %s)
                        ON CONFLICT (intent_id) DO NOTHING""",
                        (
                            intent.intent_id,
                            client_order_id,
                            intent.symbol,
                            intent.side.value,
                            intent.quantity,
                            intent.limit_price,
                            OrderState.CREATED.value,
                            moment,
                        ),
                    )
                    inserted = cursor.rowcount == 1
                    record = self._load_for_update(cursor, intent.intent_id)
                    if record.client_order_id != client_order_id:
                        raise ValueError("intent already exists with different client_order_id")
                    if inserted:
                        self._append_event(
                            cursor,
                            event_id=f"create:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.CREATED.value,
                            payload={"client_order_id": client_order_id},
                            occurred_at=moment,
                        )
                    return record

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        event_id: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> OrderRecord:
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = self._load_for_update(cursor, intent_id)
                    if self._event_exists(cursor, event_id):
                        return current
                    DurableOmsStore._validate_transition(current.state, target)
                    broker_id = (
                        current.broker_order_id
                        if broker_order_id is None
                        else broker_order_id.strip()
                    )
                    cursor.execute(
                        """UPDATE astra_oms_orders
                        SET state=%s, broker_order_id=%s, version=version+1, updated_at=%s
                        WHERE intent_id=%s""",
                        (target.value, broker_id, moment, intent_id),
                    )
                    self._append_event(
                        cursor,
                        event_id=event_id,
                        intent_id=intent_id,
                        event_type=target.value,
                        payload={} if payload is None else payload,
                        occurred_at=moment,
                    )
                    return self._load_for_update(cursor, intent_id)

    def approve_risk(self, intent_id: str, *, event_id: str, occurred_at: datetime) -> OrderRecord:
        return self.transition(
            intent_id,
            OrderState.RISK_APPROVED,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def enqueue_submit(
        self, intent_id: str, *, event_id: str, occurred_at: datetime
    ) -> OrderRecord:
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = self._load_for_update(cursor, intent_id)
                    if self._event_exists(cursor, event_id):
                        return current
                    DurableOmsStore._validate_transition(current.state, OrderState.OUTBOXED)
                    payload = {
                        "intent_id": current.intent_id,
                        "client_order_id": current.client_order_id,
                        "symbol": current.symbol,
                        "side": current.side.value,
                        "quantity": str(current.quantity),
                        "limit_price": str(current.limit_price),
                    }
                    cursor.execute(
                        """INSERT INTO astra_oms_outbox(intent_id, topic, payload, created_at)
                        VALUES (%s, 'paper_order_submit', %s::jsonb, %s)
                        ON CONFLICT (intent_id, topic) DO NOTHING""",
                        (intent_id, json.dumps(payload, sort_keys=True), moment),
                    )
                    cursor.execute(
                        """UPDATE astra_oms_orders
                        SET state=%s, version=version+1, updated_at=%s WHERE intent_id=%s""",
                        (OrderState.OUTBOXED.value, moment, intent_id),
                    )
                    self._append_event(
                        cursor,
                        event_id=event_id,
                        intent_id=intent_id,
                        event_type=OrderState.OUTBOXED.value,
                        payload=payload,
                        occurred_at=moment,
                    )
                    return self._load_for_update(cursor, intent_id)

    def pending_outbox(self, *, limit: int = 100) -> tuple[OutboxMessage, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT message_id, intent_id, topic, payload, created_at
                    FROM astra_oms_outbox WHERE published_at IS NULL
                    ORDER BY message_id LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
                return tuple(
                    OutboxMessage(
                        message_id=int(row["message_id"]),
                        intent_id=str(row["intent_id"]),
                        topic=str(row["topic"]),
                        payload=dict(row["payload"]),
                        created_at=row["created_at"],
                    )
                    for row in rows
                )

    def mark_outbox_published(self, message_id: int, *, occurred_at: datetime) -> None:
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE astra_oms_outbox
                        SET published_at=COALESCE(published_at, %s) WHERE message_id=%s""",
                        (moment, message_id),
                    )
                    if cursor.rowcount != 1:
                        raise KeyError(message_id)

    def apply_cumulative_fill(
        self,
        intent_id: str,
        *,
        event_id: str,
        cumulative_filled: Decimal,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderRecord:
        if not cumulative_filled.is_finite() or cumulative_filled < 0:
            raise ValueError("cumulative_filled must be finite and non-negative")
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = self._load_for_update(cursor, intent_id)
                    if self._event_exists(cursor, event_id):
                        return current
                    if cumulative_filled < current.filled_quantity:
                        raise ValueError("FILLED_QUANTITY_REGRESSION")
                    if cumulative_filled > current.quantity:
                        raise ValueError("FILLED_QUANTITY_EXCEEDS_ORDER")
                    if cumulative_filled == 0:
                        target = current.state
                    elif cumulative_filled == current.quantity:
                        target = OrderState.FILLED
                    else:
                        target = OrderState.PARTIALLY_FILLED
                    if target != current.state:
                        DurableOmsStore._validate_transition(current.state, target)
                    broker_id = (
                        current.broker_order_id
                        if broker_order_id is None
                        else broker_order_id.strip()
                    )
                    cursor.execute(
                        """UPDATE astra_oms_orders SET state=%s, broker_order_id=%s,
                        filled_quantity=%s, version=version+1, updated_at=%s WHERE intent_id=%s""",
                        (target.value, broker_id, cumulative_filled, moment, intent_id),
                    )
                    self._append_event(
                        cursor,
                        event_id=event_id,
                        intent_id=intent_id,
                        event_type="FILL_UPDATE",
                        payload={
                            "cumulative_filled": str(cumulative_filled),
                            "state": target.value,
                        },
                        occurred_at=moment,
                    )
                    return self._load_for_update(cursor, intent_id)

    def events(self, intent_id: str) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT event_id, event_type, payload, occurred_at
                    FROM astra_oms_events WHERE intent_id=%s ORDER BY occurred_at, event_id""",
                    (intent_id,),
                )
                rows = cursor.fetchall()
                return tuple(
                    {
                        "event_id": str(row["event_id"]),
                        "event_type": str(row["event_type"]),
                        "payload": dict(row["payload"]),
                        "occurred_at": row["occurred_at"].isoformat(),
                    }
                    for row in rows
                )
