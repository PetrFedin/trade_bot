from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_MAX_ARM_TTL = timedelta(minutes=5)
_POSTGRES_CONTROL_LOCK_KEY = 0x44535043
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class DispatchControlMode(StrEnum):
    HALTED = "HALTED"
    ARMED = "ARMED"


class DispatchControlEventKind(StrEnum):
    ARM = "ARM"
    HALT = "HALT"
    DISPATCH_AUTHORIZED = "DISPATCH_AUTHORIZED"


@dataclass(frozen=True)
class DispatchControlState:
    mode: DispatchControlMode
    version: int
    operator_id: str
    reason: str
    armed_until: datetime | None
    updated_at: datetime | None

    def allows_new_entry(self, *, occurred_at: datetime) -> bool:
        moment = _aware_utc(occurred_at, "occurred_at")
        return (
            self.mode is DispatchControlMode.ARMED
            and self.armed_until is not None
            and moment <= self.armed_until
        )


@dataclass(frozen=True)
class DispatchAuthorization:
    event_id: str
    intent_id: str
    control_version: int
    occurred_at: datetime


class DispatchBlocked(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        normalized = tuple(sorted(set(reasons)))
        if not normalized:
            raise ValueError("dispatch block requires at least one reason")
        self.reasons = normalized
        super().__init__(f"DISPATCH_BLOCKED:{','.join(normalized)}")


class PaperDispatchControlStore(Protocol):
    def current(self) -> DispatchControlState: ...

    def arm(
        self,
        *,
        operator_id: str,
        reason: str,
        occurred_at: datetime,
        ttl: timedelta = _MAX_ARM_TTL,
    ) -> DispatchControlState: ...

    def halt(
        self,
        *,
        operator_id: str,
        reason: str,
        occurred_at: datetime,
    ) -> DispatchControlState: ...

    def authorize(
        self,
        *,
        intent_id: str,
        occurred_at: datetime,
        readiness_reasons: tuple[str, ...] = (),
    ) -> DispatchAuthorization: ...

    def events(self) -> tuple[Mapping[str, object], ...]: ...


def _implicit_halt() -> DispatchControlState:
    return DispatchControlState(
        mode=DispatchControlMode.HALTED,
        version=0,
        operator_id="system",
        reason="CONTROL_NOT_ARMED",
        armed_until=None,
        updated_at=None,
    )


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{label} is too long")
    return normalized


def _validate_ttl(ttl: timedelta) -> None:
    if ttl <= timedelta(0) or ttl > _MAX_ARM_TTL:
        raise ValueError("dispatch ARM ttl must be within (0, 300] seconds")


def _assert_armed(state: DispatchControlState, *, occurred_at: datetime) -> None:
    if state.mode is not DispatchControlMode.ARMED:
        raise DispatchBlocked(("DISPATCH_CONTROL_HALTED",))
    if state.armed_until is None or occurred_at > state.armed_until:
        raise DispatchBlocked(("DISPATCH_CONTROL_EXPIRED",))


def _control_event_id(
    kind: DispatchControlEventKind,
    *,
    version: int,
    intent_id: str | None,
    occurred_at: datetime,
    payload: dict[str, object],
) -> str:
    material = json.dumps(
        {
            "kind": kind.value,
            "version": version,
            "intent_id": intent_id,
            "occurred_at": occurred_at.isoformat(),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SQLitePaperDispatchControlStore:
    """Durable fail-closed paper-dispatch control colocated with the SQLite OMS."""

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

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_dispatch_control_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    mode TEXT NOT NULL CHECK (mode IN ('HALTED', 'ARMED')),
                    version INTEGER NOT NULL CHECK (version > 0),
                    operator_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    armed_until TEXT,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (mode = 'HALTED' AND armed_until IS NULL)
                        OR (mode = 'ARMED' AND armed_until IS NOT NULL)
                    )
                );
                INSERT OR IGNORE INTO paper_dispatch_control_state
                (singleton, mode, version, operator_id, reason, armed_until, updated_at)
                VALUES (
                    1,
                    'HALTED',
                    1,
                    'system:migration',
                    'CONTROL_NOT_ARMED',
                    NULL,
                    '1970-01-01T00:00:00+00:00'
                );
                CREATE TABLE IF NOT EXISTS paper_dispatch_control_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('ARM', 'HALT', 'DISPATCH_AUTHORIZED')
                    ),
                    intent_id TEXT,
                    control_version INTEGER NOT NULL CHECK (control_version >= 0),
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS paper_dispatch_control_events_no_update
                BEFORE UPDATE ON paper_dispatch_control_events
                BEGIN
                    SELECT RAISE(ABORT, 'paper_dispatch_control_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS paper_dispatch_control_events_no_delete
                BEFORE DELETE ON paper_dispatch_control_events
                BEGIN
                    SELECT RAISE(ABORT, 'paper_dispatch_control_events is append-only');
                END;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> DispatchControlState:
        armed_until = row["armed_until"]
        return DispatchControlState(
            mode=DispatchControlMode(str(row["mode"])),
            version=int(row["version"]),
            operator_id=str(row["operator_id"]),
            reason=str(row["reason"]),
            armed_until=None
            if armed_until is None
            else _aware_utc(datetime.fromisoformat(str(armed_until)), "armed_until"),
            updated_at=_aware_utc(
                datetime.fromisoformat(str(row["updated_at"])), "updated_at"
            ),
        )

    @classmethod
    def _load_current(cls, connection: sqlite3.Connection) -> DispatchControlState:
        row = connection.execute(
            "SELECT * FROM paper_dispatch_control_state WHERE singleton=1"
        ).fetchone()
        return _implicit_halt() if row is None else cls._row(row)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        event_type: DispatchControlEventKind,
        intent_id: str | None,
        control_version: int,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO paper_dispatch_control_events
            (event_id, event_type, intent_id, control_version, payload, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event_type.value,
                intent_id,
                control_version,
                json.dumps(payload, sort_keys=True),
                occurred_at.isoformat(),
            ),
        )

    def current(self) -> DispatchControlState:
        connection = self._connect()
        try:
            return self._load_current(connection)
        finally:
            connection.close()

    def arm(
        self,
        *,
        operator_id: str,
        reason: str,
        occurred_at: datetime,
        ttl: timedelta = _MAX_ARM_TTL,
    ) -> DispatchControlState:
        moment = _aware_utc(occurred_at, "occurred_at")
        operator = _text(operator_id, "operator_id", 128)
        normalized_reason = _text(reason, "reason", 1000)
        _validate_ttl(ttl)
        armed_until = moment + ttl
        with self._transaction() as connection:
            current = self._load_current(connection)
            version = current.version + 1
            connection.execute(
                """UPDATE paper_dispatch_control_state
                SET mode=?, version=?, operator_id=?, reason=?, armed_until=?, updated_at=?
                WHERE singleton=1""",
                (
                    DispatchControlMode.ARMED.value,
                    version,
                    operator,
                    normalized_reason,
                    armed_until.isoformat(),
                    moment.isoformat(),
                ),
            )
            payload = {
                "mode": DispatchControlMode.ARMED.value,
                "operator_id": operator,
                "reason": normalized_reason,
                "armed_until": armed_until.isoformat(),
            }
            self._append_event(
                connection,
                event_id=_control_event_id(
                    DispatchControlEventKind.ARM,
                    version=version,
                    intent_id=None,
                    occurred_at=moment,
                    payload=payload,
                ),
                event_type=DispatchControlEventKind.ARM,
                intent_id=None,
                control_version=version,
                payload=payload,
                occurred_at=moment,
            )
            return self._load_current(connection)

    def halt(
        self,
        *,
        operator_id: str,
        reason: str,
        occurred_at: datetime,
    ) -> DispatchControlState:
        moment = _aware_utc(occurred_at, "occurred_at")
        operator = _text(operator_id, "operator_id", 128)
        normalized_reason = _text(reason, "reason", 1000)
        with self._transaction() as connection:
            current = self._load_current(connection)
            version = current.version + 1
            connection.execute(
                """UPDATE paper_dispatch_control_state
                SET mode=?, version=?, operator_id=?, reason=?, armed_until=NULL, updated_at=?
                WHERE singleton=1""",
                (
                    DispatchControlMode.HALTED.value,
                    version,
                    operator,
                    normalized_reason,
                    moment.isoformat(),
                ),
            )
            payload = {
                "mode": DispatchControlMode.HALTED.value,
                "operator_id": operator,
                "reason": normalized_reason,
            }
            self._append_event(
                connection,
                event_id=_control_event_id(
                    DispatchControlEventKind.HALT,
                    version=version,
                    intent_id=None,
                    occurred_at=moment,
                    payload=payload,
                ),
                event_type=DispatchControlEventKind.HALT,
                intent_id=None,
                control_version=version,
                payload=payload,
                occurred_at=moment,
            )
            return self._load_current(connection)

    def authorize(
        self,
        *,
        intent_id: str,
        occurred_at: datetime,
        readiness_reasons: tuple[str, ...] = (),
    ) -> DispatchAuthorization:
        moment = _aware_utc(occurred_at, "occurred_at")
        normalized_intent = _text(intent_id, "intent_id", 256)
        if readiness_reasons:
            raise DispatchBlocked(tuple(readiness_reasons))
        with self._transaction() as connection:
            current = self._load_current(connection)
            _assert_armed(current, occurred_at=moment)
            payload = {
                "intent_id": normalized_intent,
                "control_version": current.version,
                "readiness_reasons": [],
            }
            event_id = _control_event_id(
                DispatchControlEventKind.DISPATCH_AUTHORIZED,
                version=current.version,
                intent_id=normalized_intent,
                occurred_at=moment,
                payload=payload,
            )
            self._append_event(
                connection,
                event_id=event_id,
                event_type=DispatchControlEventKind.DISPATCH_AUTHORIZED,
                intent_id=normalized_intent,
                control_version=current.version,
                payload=payload,
                occurred_at=moment,
            )
            return DispatchAuthorization(
                event_id=event_id,
                intent_id=normalized_intent,
                control_version=current.version,
                occurred_at=moment,
            )

    def events(self) -> tuple[dict[str, object], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT event_id, event_type, intent_id, control_version,
                payload, occurred_at
                FROM paper_dispatch_control_events ORDER BY rowid"""
            ).fetchall()
            return tuple(
                {
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "intent_id": None
                    if row["intent_id"] is None
                    else str(row["intent_id"]),
                    "control_version": int(row["control_version"]),
                    "payload": dict(json.loads(str(row["payload"]))),
                    "occurred_at": str(row["occurred_at"]),
                }
                for row in rows
            )
        finally:
            connection.close()


class PostgresPaperDispatchControlStore:
    """PostgreSQL equivalent with explicit account-wide control serialization."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError(
                "install the postgresql extra to use PostgresPaperDispatchControlStore"
            )
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self, path: str | Path = "migrations/product/006_paper_dispatch_control.sql"
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _row(row: Mapping[str, object]) -> DispatchControlState:
        armed_until = row["armed_until"]
        updated_at = row["updated_at"]
        if not isinstance(updated_at, datetime):
            updated_at = datetime.fromisoformat(str(updated_at))
        if armed_until is not None and not isinstance(armed_until, datetime):
            armed_until = datetime.fromisoformat(str(armed_until))
        return DispatchControlState(
            mode=DispatchControlMode(str(row["mode"])),
            version=int(row["version"]),
            operator_id=str(row["operator_id"]),
            reason=str(row["reason"]),
            armed_until=None
            if armed_until is None
            else _aware_utc(armed_until, "armed_until"),
            updated_at=_aware_utc(updated_at, "updated_at"),
        )

    @classmethod
    def _load_current(cls, cursor, *, for_update: bool) -> DispatchControlState:
        if for_update:
            cursor.execute(
                """SELECT * FROM astra_paper_dispatch_control_state
                WHERE singleton=TRUE FOR UPDATE"""
            )
        else:
            cursor.execute(
                """SELECT * FROM astra_paper_dispatch_control_state
                WHERE singleton=TRUE"""
            )
        row = cursor.fetchone()
        return _implicit_halt() if row is None else cls._row(row)

    @staticmethod
    def _lock_control(cursor) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_POSTGRES_CONTROL_LOCK_KEY,),
        )

    @staticmethod
    def _append_event(
        cursor,
        *,
        event_id: str,
        event_type: DispatchControlEventKind,
        intent_id: str | None,
        control_version: int,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        cursor.execute(
            """INSERT INTO astra_paper_dispatch_control_events
            (event_id, event_type, intent_id, control_version, payload, occurred_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING""",
            (
                event_id,
                event_type.value,
                intent_id,
                control_version,
                json.dumps(payload, sort_keys=True),
                occurred_at,
            ),
        )

    def current(self) -> DispatchControlState:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                return self._load_current(cursor, for_update=False)

    def arm(
        self,
        *,
        operator_id: str,
        reason: str,
        occurred_at: datetime,
        ttl: timedelta = _MAX_ARM_TTL,
    ) -> DispatchControlState:
        moment = _aware_utc(occurred_at, "occurred_at")
        operator = _text(operator_id, "operator_id", 128)
        normalized_reason = _text(reason, "reason", 1000)
        _validate_ttl(ttl)
        armed_until = moment + ttl
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    self._lock_control(cursor)
                    current = self._load_current(cursor, for_update=True)
                    version = current.version + 1
                    cursor.execute(
                        """INSERT INTO astra_paper_dispatch_control_state
                        (singleton, mode, version, operator_id, reason, armed_until, updated_at)
                        VALUES (TRUE, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (singleton) DO UPDATE SET
                            mode=EXCLUDED.mode,
                            version=EXCLUDED.version,
                            operator_id=EXCLUDED.operator_id,
                            reason=EXCLUDED.reason,
                            armed_until=EXCLUDED.armed_until,
                            updated_at=EXCLUDED.updated_at""",
                        (
                            DispatchControlMode.ARMED.value,
                            version,
                            operator,
                            normalized_reason,
                            armed_until,
                            moment,
                        ),
                    )
                    payload = {
                        "mode": DispatchControlMode.ARMED.value,
                        "operator_id": operator,
                        "reason": normalized_reason,
                        "armed_until": armed_until.isoformat(),
                    }
                    self._append_event(
                        cursor,
                        event_id=_control_event_id(
                            DispatchControlEventKind.ARM,
                            version=version,
                            intent_id=None,
                            occurred_at=moment,
                            payload=payload,
                        ),
                        event_type=DispatchControlEventKind.ARM,
                        intent_id=None,
                        control_version=version,
                        payload=payload,
                        occurred_at=moment,
                    )
                    return self._load_current(cursor, for_update=False)

    def halt(
        self,
        *,
        operator_id: str,
        reason: str,
        occurred_at: datetime,
    ) -> DispatchControlState:
        moment = _aware_utc(occurred_at, "occurred_at")
        operator = _text(operator_id, "operator_id", 128)
        normalized_reason = _text(reason, "reason", 1000)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    self._lock_control(cursor)
                    current = self._load_current(cursor, for_update=True)
                    version = current.version + 1
                    cursor.execute(
                        """INSERT INTO astra_paper_dispatch_control_state
                        (singleton, mode, version, operator_id, reason, armed_until, updated_at)
                        VALUES (TRUE, %s, %s, %s, %s, NULL, %s)
                        ON CONFLICT (singleton) DO UPDATE SET
                            mode=EXCLUDED.mode,
                            version=EXCLUDED.version,
                            operator_id=EXCLUDED.operator_id,
                            reason=EXCLUDED.reason,
                            armed_until=NULL,
                            updated_at=EXCLUDED.updated_at""",
                        (
                            DispatchControlMode.HALTED.value,
                            version,
                            operator,
                            normalized_reason,
                            moment,
                        ),
                    )
                    payload = {
                        "mode": DispatchControlMode.HALTED.value,
                        "operator_id": operator,
                        "reason": normalized_reason,
                    }
                    self._append_event(
                        cursor,
                        event_id=_control_event_id(
                            DispatchControlEventKind.HALT,
                            version=version,
                            intent_id=None,
                            occurred_at=moment,
                            payload=payload,
                        ),
                        event_type=DispatchControlEventKind.HALT,
                        intent_id=None,
                        control_version=version,
                        payload=payload,
                        occurred_at=moment,
                    )
                    return self._load_current(cursor, for_update=False)

    def authorize(
        self,
        *,
        intent_id: str,
        occurred_at: datetime,
        readiness_reasons: tuple[str, ...] = (),
    ) -> DispatchAuthorization:
        moment = _aware_utc(occurred_at, "occurred_at")
        normalized_intent = _text(intent_id, "intent_id", 256)
        if readiness_reasons:
            raise DispatchBlocked(tuple(readiness_reasons))
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    self._lock_control(cursor)
                    current = self._load_current(cursor, for_update=True)
                    _assert_armed(current, occurred_at=moment)
                    payload = {
                        "intent_id": normalized_intent,
                        "control_version": current.version,
                        "readiness_reasons": [],
                    }
                    event_id = _control_event_id(
                        DispatchControlEventKind.DISPATCH_AUTHORIZED,
                        version=current.version,
                        intent_id=normalized_intent,
                        occurred_at=moment,
                        payload=payload,
                    )
                    self._append_event(
                        cursor,
                        event_id=event_id,
                        event_type=DispatchControlEventKind.DISPATCH_AUTHORIZED,
                        intent_id=normalized_intent,
                        control_version=current.version,
                        payload=payload,
                        occurred_at=moment,
                    )
                    return DispatchAuthorization(
                        event_id=event_id,
                        intent_id=normalized_intent,
                        control_version=current.version,
                        occurred_at=moment,
                    )

    def events(self) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT event_id, event_type, intent_id, control_version,
                    payload, occurred_at
                    FROM astra_paper_dispatch_control_events
                    ORDER BY sequence"""
                )
                rows = cursor.fetchall()
                return tuple(
                    {
                        "event_id": str(row["event_id"]),
                        "event_type": str(row["event_type"]),
                        "intent_id": None
                        if row["intent_id"] is None
                        else str(row["intent_id"]),
                        "control_version": int(row["control_version"]),
                        "payload": dict(row["payload"]),
                        "occurred_at": row["occurred_at"].isoformat(),
                    }
                    for row in rows
                )
