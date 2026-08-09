from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.oms.order_mutations import (
    ActiveMutationExists,
    MutationKind,
    MutationOutboxMessage,
    MutationState,
    OrderMutationRecord,
    _same_request,
    _validate_request,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresOrderMutationStore:
    """PostgreSQL mutation journal/outbox with row locking and active-operation fencing."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use PostgresOrderMutationStore")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/004_order_mutations.sql") -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _now(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _row(row: Mapping[str, object]) -> OrderMutationRecord:
        target = row["target_limit_price"]
        created_at = row["created_at"]
        updated_at = row["updated_at"]
        if not isinstance(created_at, datetime):
            created_at = datetime.fromisoformat(str(created_at))
        if not isinstance(updated_at, datetime):
            updated_at = datetime.fromisoformat(str(updated_at))
        return OrderMutationRecord(
            mutation_id=str(row["mutation_id"]),
            intent_id=str(row["intent_id"]),
            kind=MutationKind(str(row["kind"])),
            target_limit_price=None if target is None else Decimal(str(target)),
            baseline_limit_price=Decimal(str(row["baseline_limit_price"])),
            broker_order_id=str(row["broker_order_id"]),
            state=MutationState(str(row["state"])),
            outcome=str(row["outcome"]),
            version=int(row["version"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _load(cursor, mutation_id: str) -> OrderMutationRecord:
        cursor.execute(
            "SELECT * FROM astra_order_mutations WHERE mutation_id=%s FOR UPDATE",
            (mutation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(mutation_id)
        return PostgresOrderMutationStore._row(row)

    @staticmethod
    def _append_event(
        cursor,
        *,
        event_id: str,
        mutation_id: str,
        event_type: str,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        cursor.execute(
            """INSERT INTO astra_order_mutation_events
            (event_id, mutation_id, event_type, payload, occurred_at)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING""",
            (event_id, mutation_id, event_type, json.dumps(payload, sort_keys=True), occurred_at),
        )

    def get(self, mutation_id: str) -> OrderMutationRecord | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM astra_order_mutations WHERE mutation_id=%s", (mutation_id,)
                )
                row = cursor.fetchone()
                return None if row is None else self._row(row)

    def request(
        self,
        *,
        mutation_id: str,
        intent_id: str,
        kind: MutationKind,
        target_limit_price: Decimal | None,
        baseline_limit_price: Decimal,
        broker_order_id: str,
        occurred_at: datetime,
    ) -> OrderMutationRecord:
        _validate_request(
            mutation_id=mutation_id,
            intent_id=intent_id,
            kind=kind,
            target_limit_price=target_limit_price,
            baseline_limit_price=baseline_limit_price,
            broker_order_id=broker_order_id,
        )
        moment = self._now(occurred_at)
        with self._connect() as connection:
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT * FROM astra_order_mutations WHERE mutation_id=%s FOR UPDATE",
                            (mutation_id,),
                        )
                        row = cursor.fetchone()
                        if row is not None:
                            existing = self._row(row)
                            if not _same_request(
                                existing,
                                intent_id=intent_id,
                                kind=kind,
                                target_limit_price=target_limit_price,
                                baseline_limit_price=baseline_limit_price,
                                broker_order_id=broker_order_id,
                            ):
                                raise ValueError("MUTATION_ID_CONFLICT")
                            return existing
                        cursor.execute(
                            """INSERT INTO astra_order_mutations
                            (mutation_id, intent_id, kind, target_limit_price,
                             baseline_limit_price, broker_order_id, state, outcome,
                             version, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, '', 1, %s, %s)""",
                            (
                                mutation_id,
                                intent_id,
                                kind.value,
                                target_limit_price,
                                baseline_limit_price,
                                broker_order_id,
                                MutationState.REQUESTED.value,
                                moment,
                                moment,
                            ),
                        )
                        payload: dict[str, object] = {
                            "mutation_id": mutation_id,
                            "intent_id": intent_id,
                            "kind": kind.value,
                            "broker_order_id": broker_order_id,
                            "baseline_limit_price": str(baseline_limit_price),
                        }
                        if target_limit_price is not None:
                            payload["target_limit_price"] = str(target_limit_price)
                        topic = (
                            "paper_order_cancel"
                            if kind is MutationKind.CANCEL
                            else "paper_order_replace"
                        )
                        cursor.execute(
                            """INSERT INTO astra_order_mutation_outbox
                            (mutation_id, intent_id, topic, payload, created_at)
                            VALUES (%s, %s, %s, %s::jsonb, %s)""",
                            (
                                mutation_id,
                                intent_id,
                                topic,
                                json.dumps(payload, sort_keys=True),
                                moment,
                            ),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"request:{mutation_id}",
                            mutation_id=mutation_id,
                            event_type=MutationState.REQUESTED.value,
                            payload=payload,
                            occurred_at=moment,
                        )
                        return self._load(cursor, mutation_id)
            except Exception as exc:
                if psycopg is not None and isinstance(exc, psycopg.errors.UniqueViolation):
                    raise ActiveMutationExists("ACTIVE_MUTATION_EXISTS") from exc
                raise

    def _set_state(
        self,
        mutation_id: str,
        target: MutationState,
        *,
        outcome: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderMutationRecord:
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = self._load(cursor, mutation_id)
                    if current.state is target and current.outcome == outcome:
                        return current
                    allowed = {
                        MutationState.REQUESTED: {MutationState.STARTED},
                        MutationState.STARTED: {
                            MutationState.SUCCEEDED,
                            MutationState.FAILED,
                            MutationState.UNCERTAIN,
                        },
                        MutationState.UNCERTAIN: {
                            MutationState.SUCCEEDED,
                            MutationState.FAILED,
                            MutationState.UNCERTAIN,
                        },
                        MutationState.SUCCEEDED: set(),
                        MutationState.FAILED: set(),
                    }
                    if target not in allowed[current.state]:
                        raise ValueError(
                            f"invalid mutation transition: {current.state.value}->{target.value}"
                        )
                    resolved_broker_id = (
                        current.broker_order_id
                        if broker_order_id is None
                        else broker_order_id.strip()
                    )
                    if not resolved_broker_id:
                        raise ValueError("broker_order_id is required")
                    cursor.execute(
                        """UPDATE astra_order_mutations
                        SET state=%s, outcome=%s, broker_order_id=%s,
                            version=version+1, updated_at=%s
                        WHERE mutation_id=%s""",
                        (target.value, outcome, resolved_broker_id, moment, mutation_id),
                    )
                    self._append_event(
                        cursor,
                        event_id=f"state:{mutation_id}:{target.value}:{current.version + 1}",
                        mutation_id=mutation_id,
                        event_type=target.value,
                        payload={"outcome": outcome},
                        occurred_at=moment,
                    )
                    return self._load(cursor, mutation_id)

    def mark_started(self, mutation_id: str, *, occurred_at: datetime) -> OrderMutationRecord:
        return self._set_state(
            mutation_id, MutationState.STARTED, outcome="", occurred_at=occurred_at
        )

    def mark_succeeded(
        self,
        mutation_id: str,
        *,
        outcome: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderMutationRecord:
        return self._set_state(
            mutation_id,
            MutationState.SUCCEEDED,
            outcome=outcome,
            occurred_at=occurred_at,
            broker_order_id=broker_order_id,
        )

    def mark_failed(
        self, mutation_id: str, *, outcome: str, occurred_at: datetime
    ) -> OrderMutationRecord:
        return self._set_state(
            mutation_id, MutationState.FAILED, outcome=outcome, occurred_at=occurred_at
        )

    def mark_uncertain(
        self, mutation_id: str, *, outcome: str, occurred_at: datetime
    ) -> OrderMutationRecord:
        return self._set_state(
            mutation_id, MutationState.UNCERTAIN, outcome=outcome, occurred_at=occurred_at
        )

    def pending_outbox(self, *, limit: int = 100) -> tuple[MutationOutboxMessage, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT message_id, mutation_id, intent_id, topic, payload, created_at
                    FROM astra_order_mutation_outbox
                    WHERE published_at IS NULL ORDER BY message_id LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
                return tuple(
                    MutationOutboxMessage(
                        message_id=int(row["message_id"]),
                        mutation_id=str(row["mutation_id"]),
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
                        """UPDATE astra_order_mutation_outbox
                        SET published_at=COALESCE(published_at, %s) WHERE message_id=%s""",
                        (moment, message_id),
                    )
                    if cursor.rowcount != 1:
                        raise KeyError(message_id)

    def current_limit_price(self, intent_id: str, *, fallback: Decimal) -> Decimal:
        if not fallback.is_finite() or fallback <= 0:
            raise ValueError("fallback must be positive and finite")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT target_limit_price FROM astra_order_mutations
                    WHERE intent_id=%s AND kind='REPLACE' AND state='SUCCEEDED'
                    ORDER BY updated_at DESC, mutation_id DESC LIMIT 1""",
                    (intent_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return fallback
                return Decimal(str(row["target_limit_price"]))

    def current_broker_order_id(self, intent_id: str, *, fallback: str) -> str:
        if not fallback.strip():
            raise ValueError("fallback broker_order_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT broker_order_id FROM astra_order_mutations
                    WHERE intent_id=%s AND kind='REPLACE' AND state='SUCCEEDED'
                    ORDER BY updated_at DESC, mutation_id DESC LIMIT 1""",
                    (intent_id,),
                )
                row = cursor.fetchone()
                return fallback if row is None else str(row["broker_order_id"])

    def events(self, mutation_id: str) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT event_id, event_type, payload, occurred_at
                    FROM astra_order_mutation_events WHERE mutation_id=%s
                    ORDER BY occurred_at, event_id""",
                    (mutation_id,),
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
