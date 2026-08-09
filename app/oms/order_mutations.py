from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.oms.store import OrderRecord, OrderState


class MutationKind(StrEnum):
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


class MutationState(StrEnum):
    REQUESTED = "REQUESTED"
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


_FINAL_MUTATION_STATES = frozenset({MutationState.SUCCEEDED, MutationState.FAILED})
_ACTIVE_ORDER_STATES = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})


class ActiveMutationExists(ValueError):
    pass


@dataclass(frozen=True)
class OrderMutationRecord:
    mutation_id: str
    intent_id: str
    kind: MutationKind
    target_limit_price: Decimal | None
    baseline_limit_price: Decimal
    broker_order_id: str
    state: MutationState
    outcome: str
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.state in _FINAL_MUTATION_STATES


@dataclass(frozen=True)
class MutationOutboxMessage:
    message_id: int
    mutation_id: str
    intent_id: str
    topic: str
    payload: dict[str, object]
    created_at: datetime


class MutationStore(Protocol):
    def get(self, mutation_id: str) -> OrderMutationRecord | None: ...

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
    ) -> OrderMutationRecord: ...

    def mark_started(self, mutation_id: str, *, occurred_at: datetime) -> OrderMutationRecord: ...

    def mark_succeeded(
        self,
        mutation_id: str,
        *,
        outcome: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderMutationRecord: ...

    def mark_failed(
        self,
        mutation_id: str,
        *,
        outcome: str,
        occurred_at: datetime,
    ) -> OrderMutationRecord: ...

    def mark_uncertain(
        self,
        mutation_id: str,
        *,
        outcome: str,
        occurred_at: datetime,
    ) -> OrderMutationRecord: ...

    def pending_outbox(self, *, limit: int = 100) -> tuple[MutationOutboxMessage, ...]: ...

    def mark_outbox_published(self, message_id: int, *, occurred_at: datetime) -> None: ...

    def current_limit_price(self, intent_id: str, *, fallback: Decimal) -> Decimal: ...

    def current_broker_order_id(self, intent_id: str, *, fallback: str) -> str: ...

    def events(self, mutation_id: str) -> tuple[Mapping[str, object], ...]: ...


class OrderMutationLifecycle:
    """Durably request broker cancel/replace operations before any network mutation."""

    def __init__(self, *, oms, mutations: MutationStore) -> None:
        self.oms = oms
        self.mutations = mutations

    def request_cancel(
        self,
        intent_id: str,
        *,
        mutation_id: str,
        occurred_at: datetime,
    ) -> OrderMutationRecord:
        existing = self.mutations.get(mutation_id)
        if existing is not None:
            if existing.intent_id != intent_id or existing.kind is not MutationKind.CANCEL:
                raise ValueError("MUTATION_ID_CONFLICT")
            return existing
        order = self._active_order(intent_id)
        return self.mutations.request(
            mutation_id=mutation_id,
            intent_id=intent_id,
            kind=MutationKind.CANCEL,
            target_limit_price=None,
            baseline_limit_price=self.mutations.current_limit_price(
                intent_id, fallback=order.limit_price
            ),
            broker_order_id=self.mutations.current_broker_order_id(
                intent_id, fallback=order.broker_order_id
            ),
            occurred_at=occurred_at,
        )

    def request_replace(
        self,
        intent_id: str,
        *,
        mutation_id: str,
        target_limit_price: Decimal,
        occurred_at: datetime,
    ) -> OrderMutationRecord:
        if not target_limit_price.is_finite() or target_limit_price <= 0:
            raise ValueError("target_limit_price must be positive and finite")
        existing = self.mutations.get(mutation_id)
        if existing is not None:
            if (
                existing.intent_id != intent_id
                or existing.kind is not MutationKind.REPLACE
                or existing.target_limit_price != target_limit_price
            ):
                raise ValueError("MUTATION_ID_CONFLICT")
            return existing
        order = self._active_order(intent_id)
        baseline = self.mutations.current_limit_price(intent_id, fallback=order.limit_price)
        if target_limit_price == baseline:
            raise ValueError("REPLACE_PRICE_UNCHANGED")
        return self.mutations.request(
            mutation_id=mutation_id,
            intent_id=intent_id,
            kind=MutationKind.REPLACE,
            target_limit_price=target_limit_price,
            baseline_limit_price=baseline,
            broker_order_id=self.mutations.current_broker_order_id(
                intent_id, fallback=order.broker_order_id
            ),
            occurred_at=occurred_at,
        )

    def _active_order(self, intent_id: str) -> OrderRecord:
        if not mutation_id_safe(intent_id):
            raise ValueError("intent_id is required")
        order = self.oms.get(intent_id)
        if order is None:
            raise KeyError(intent_id)
        if order.state not in _ACTIVE_ORDER_STATES:
            raise ValueError(f"ORDER_NOT_MUTABLE:{order.state.value}")
        if not order.broker_order_id.strip():
            raise ValueError("BROKER_ORDER_ID_REQUIRED")
        return order


def mutation_id_safe(value: str) -> bool:
    return bool(value.strip())


def _validate_request(
    *,
    mutation_id: str,
    intent_id: str,
    kind: MutationKind,
    target_limit_price: Decimal | None,
    baseline_limit_price: Decimal,
    broker_order_id: str,
) -> None:
    if not mutation_id_safe(mutation_id) or not mutation_id_safe(intent_id):
        raise ValueError("mutation_id and intent_id are required")
    if not broker_order_id.strip():
        raise ValueError("broker_order_id is required")
    if not baseline_limit_price.is_finite() or baseline_limit_price <= 0:
        raise ValueError("baseline_limit_price must be positive and finite")
    if kind is MutationKind.CANCEL and target_limit_price is not None:
        raise ValueError("cancel mutation cannot have target_limit_price")
    if kind is MutationKind.REPLACE:
        if target_limit_price is None:
            raise ValueError("replace mutation requires target_limit_price")
        if not target_limit_price.is_finite() or target_limit_price <= 0:
            raise ValueError("target_limit_price must be positive and finite")


def _same_request(
    record: OrderMutationRecord,
    *,
    intent_id: str,
    kind: MutationKind,
    target_limit_price: Decimal | None,
    baseline_limit_price: Decimal,
    broker_order_id: str,
) -> bool:
    return (
        record.intent_id == intent_id
        and record.kind is kind
        and record.target_limit_price == target_limit_price
        and record.baseline_limit_price == baseline_limit_price
        and record.broker_order_id == broker_order_id
    )


class DurableOrderMutationStore:
    """SQLite mutation journal/outbox colocated with the durable OMS database."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _now(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oms_order_mutations (
                    mutation_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES oms_orders(intent_id) ON DELETE RESTRICT,
                    kind TEXT NOT NULL CHECK (kind IN ('CANCEL', 'REPLACE')),
                    target_limit_price TEXT,
                    baseline_limit_price TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('REQUESTED', 'STARTED', 'SUCCEEDED', 'FAILED', 'UNCERTAIN')
                    ),
                    outcome TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (kind = 'CANCEL' AND target_limit_price IS NULL)
                        OR (kind = 'REPLACE' AND target_limit_price IS NOT NULL)
                    )
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_oms_order_mutations_one_active
                    ON oms_order_mutations(intent_id)
                    WHERE state IN ('REQUESTED', 'STARTED', 'UNCERTAIN');
                CREATE TABLE IF NOT EXISTS oms_order_mutation_events (
                    event_id TEXT PRIMARY KEY,
                    mutation_id TEXT NOT NULL
                        REFERENCES oms_order_mutations(mutation_id) ON DELETE RESTRICT,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS oms_order_mutation_events_no_update
                BEFORE UPDATE ON oms_order_mutation_events
                BEGIN
                    SELECT RAISE(ABORT, 'oms_order_mutation_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS oms_order_mutation_events_no_delete
                BEFORE DELETE ON oms_order_mutation_events
                BEGIN
                    SELECT RAISE(ABORT, 'oms_order_mutation_events is append-only');
                END;
                CREATE TABLE IF NOT EXISTS oms_order_mutation_outbox (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mutation_id TEXT NOT NULL UNIQUE
                        REFERENCES oms_order_mutations(mutation_id) ON DELETE RESTRICT,
                    intent_id TEXT NOT NULL REFERENCES oms_orders(intent_id) ON DELETE RESTRICT,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_oms_order_mutation_outbox_pending
                    ON oms_order_mutation_outbox(message_id)
                    WHERE published_at IS NULL;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> OrderMutationRecord:
        target = row["target_limit_price"]
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
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _load(connection: sqlite3.Connection, mutation_id: str) -> OrderMutationRecord:
        row = connection.execute(
            "SELECT * FROM oms_order_mutations WHERE mutation_id=?", (mutation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(mutation_id)
        return DurableOrderMutationStore._row(row)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        mutation_id: str,
        event_type: str,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO oms_order_mutation_events
            (event_id, mutation_id, event_type, payload, occurred_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                event_id,
                mutation_id,
                event_type,
                json.dumps(payload, sort_keys=True),
                occurred_at.isoformat(),
            ),
        )

    def get(self, mutation_id: str) -> OrderMutationRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM oms_order_mutations WHERE mutation_id=?", (mutation_id,)
            ).fetchone()
            return None if row is None else self._row(row)
        finally:
            connection.close()

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
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM oms_order_mutations WHERE mutation_id=?", (mutation_id,)
            ).fetchone()
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
            try:
                connection.execute(
                    """INSERT INTO oms_order_mutations
                    (mutation_id, intent_id, kind, target_limit_price, baseline_limit_price,
                     broker_order_id, state, outcome, version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', 1, ?, ?)""",
                    (
                        mutation_id,
                        intent_id,
                        kind.value,
                        None if target_limit_price is None else str(target_limit_price),
                        str(baseline_limit_price),
                        broker_order_id,
                        MutationState.REQUESTED.value,
                        moment.isoformat(),
                        moment.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "oms_order_mutations.intent_id" in str(exc):
                    raise ActiveMutationExists("ACTIVE_MUTATION_EXISTS") from exc
                raise
            payload: dict[str, object] = {
                "mutation_id": mutation_id,
                "intent_id": intent_id,
                "kind": kind.value,
                "broker_order_id": broker_order_id,
                "baseline_limit_price": str(baseline_limit_price),
            }
            if target_limit_price is not None:
                payload["target_limit_price"] = str(target_limit_price)
            topic = "paper_order_cancel" if kind is MutationKind.CANCEL else "paper_order_replace"
            connection.execute(
                """INSERT INTO oms_order_mutation_outbox
                (mutation_id, intent_id, topic, payload, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    mutation_id,
                    intent_id,
                    topic,
                    json.dumps(payload, sort_keys=True),
                    moment.isoformat(),
                ),
            )
            self._append_event(
                connection,
                event_id=f"request:{mutation_id}",
                mutation_id=mutation_id,
                event_type=MutationState.REQUESTED.value,
                payload=payload,
                occurred_at=moment,
            )
            return self._load(connection, mutation_id)

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
        with self._transaction() as connection:
            current = self._load(connection, mutation_id)
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
            connection.execute(
                """UPDATE oms_order_mutations
                SET state=?, outcome=?, broker_order_id=?, version=version+1, updated_at=?
                WHERE mutation_id=?""",
                (
                    target.value,
                    outcome,
                    resolved_broker_id,
                    moment.isoformat(),
                    mutation_id,
                ),
            )
            self._append_event(
                connection,
                event_id=f"state:{mutation_id}:{target.value}:{current.version + 1}",
                mutation_id=mutation_id,
                event_type=target.value,
                payload={"outcome": outcome},
                occurred_at=moment,
            )
            return self._load(connection, mutation_id)

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
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT message_id, mutation_id, intent_id, topic, payload, created_at
                FROM oms_order_mutation_outbox
                WHERE published_at IS NULL ORDER BY message_id LIMIT ?""",
                (limit,),
            ).fetchall()
            return tuple(
                MutationOutboxMessage(
                    message_id=int(row["message_id"]),
                    mutation_id=str(row["mutation_id"]),
                    intent_id=str(row["intent_id"]),
                    topic=str(row["topic"]),
                    payload=dict(json.loads(str(row["payload"]))),
                    created_at=datetime.fromisoformat(str(row["created_at"])),
                )
                for row in rows
            )
        finally:
            connection.close()

    def mark_outbox_published(self, message_id: int, *, occurred_at: datetime) -> None:
        moment = self._now(occurred_at)
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE oms_order_mutation_outbox
                SET published_at=COALESCE(published_at, ?) WHERE message_id=?""",
                (moment.isoformat(), message_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(message_id)

    def current_limit_price(self, intent_id: str, *, fallback: Decimal) -> Decimal:
        if not fallback.is_finite() or fallback <= 0:
            raise ValueError("fallback must be positive and finite")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT target_limit_price FROM oms_order_mutations
                WHERE intent_id=? AND kind='REPLACE' AND state='SUCCEEDED'
                ORDER BY updated_at DESC, mutation_id DESC LIMIT 1""",
                (intent_id,),
            ).fetchone()
            if row is None:
                return fallback
            return Decimal(str(row["target_limit_price"]))
        finally:
            connection.close()

    def current_broker_order_id(self, intent_id: str, *, fallback: str) -> str:
        if not fallback.strip():
            raise ValueError("fallback broker_order_id is required")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT broker_order_id FROM oms_order_mutations
                WHERE intent_id=? AND kind='REPLACE' AND state='SUCCEEDED'
                ORDER BY updated_at DESC, mutation_id DESC LIMIT 1""",
                (intent_id,),
            ).fetchone()
            return fallback if row is None else str(row["broker_order_id"])
        finally:
            connection.close()

    def events(self, mutation_id: str) -> tuple[dict[str, object], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT event_id, event_type, payload, occurred_at
                FROM oms_order_mutation_events
                WHERE mutation_id=? ORDER BY rowid""",
                (mutation_id,),
            ).fetchall()
            return tuple(
                {
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "payload": dict(json.loads(str(row["payload"]))),
                    "occurred_at": str(row["occurred_at"]),
                }
                for row in rows
            )
        finally:
            connection.close()
