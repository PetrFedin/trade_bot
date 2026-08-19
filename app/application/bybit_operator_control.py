from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class BybitOperatorMode(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    READ_ONLY = "READ_ONLY"
    KILLED = "KILLED"


@dataclass(frozen=True)
class BybitOperatorSnapshot:
    mode: BybitOperatorMode
    generation: int
    updated_at: datetime
    updated_by: str
    reason: str
    live_mainnet_order_routing_allowed: bool = False
    active_trade_safety_management_allowed: bool = True

    @property
    def new_entries_allowed(self) -> bool:
        return self.mode is BybitOperatorMode.RUNNING

    @property
    def read_only_mode(self) -> bool:
        return self.mode is BybitOperatorMode.READ_ONLY

    @property
    def kill_switch_engaged(self) -> bool:
        return self.mode is BybitOperatorMode.KILLED


@dataclass(frozen=True)
class BybitOperatorAction:
    action_id: str
    generation: int
    from_mode: BybitOperatorMode
    to_mode: BybitOperatorMode
    actor: str
    reason: str
    occurred_at: datetime


class PostgresBybitOperatorControl:
    """Durable fail-closed operator state with append-only action audit.

    This store never talks to a broker and never authorizes live/mainnet routing. Operator modes
    gate new entries only; active-trade protection/management is intentionally always allowed so a
    pause or kill command cannot abandon an existing position.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    active_trade_safety_management_allowed = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use Bybit operator control")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/product/006_bybit_operator_control.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def inspect(self) -> BybitOperatorSnapshot:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM astra_bybit_operator_state WHERE singleton=TRUE")
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Bybit operator state is not initialized")
        return _snapshot(row)

    def history(self, *, limit: int = 100) -> tuple[BybitOperatorAction, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("operator action history limit must be within [1, 1000]")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM astra_bybit_operator_actions
                    ORDER BY generation DESC LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
        return tuple(_action(row) for row in rows)

    def pause(
        self,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None = None,
        action_id: str | None = None,
    ) -> BybitOperatorSnapshot:
        return self._transition(
            BybitOperatorMode.PAUSED,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            action_id=action_id,
            allowed_from={
                BybitOperatorMode.RUNNING,
                BybitOperatorMode.PAUSED,
                BybitOperatorMode.READ_ONLY,
            },
        )

    def enter_read_only(
        self,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None = None,
        action_id: str | None = None,
    ) -> BybitOperatorSnapshot:
        return self._transition(
            BybitOperatorMode.READ_ONLY,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            action_id=action_id,
            allowed_from={
                BybitOperatorMode.RUNNING,
                BybitOperatorMode.PAUSED,
                BybitOperatorMode.READ_ONLY,
            },
        )

    def kill(
        self,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None = None,
        action_id: str | None = None,
    ) -> BybitOperatorSnapshot:
        return self._transition(
            BybitOperatorMode.KILLED,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            action_id=action_id,
            allowed_from=set(BybitOperatorMode),
        )

    def resume(
        self,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None = None,
        action_id: str | None = None,
    ) -> BybitOperatorSnapshot:
        return self._transition(
            BybitOperatorMode.RUNNING,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            action_id=action_id,
            allowed_from={
                BybitOperatorMode.RUNNING,
                BybitOperatorMode.PAUSED,
                BybitOperatorMode.READ_ONLY,
            },
        )

    def clear_kill(
        self,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None = None,
        action_id: str | None = None,
    ) -> BybitOperatorSnapshot:
        return self._transition(
            BybitOperatorMode.PAUSED,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            action_id=action_id,
            allowed_from={BybitOperatorMode.KILLED},
            same_target_is_noop=False,
        )

    def _transition(
        self,
        target: BybitOperatorMode,
        *,
        actor: str,
        reason: str,
        occurred_at: datetime | None,
        action_id: str | None,
        allowed_from: set[BybitOperatorMode],
        same_target_is_noop: bool = True,
    ) -> BybitOperatorSnapshot:
        actor_value = _bounded_text(actor, name="operator actor", maximum=128)
        reason_value = _bounded_text(reason, name="operator reason", maximum=512)
        action_value = _bounded_text(
            action_id or f"operator-{secrets.token_hex(16)}",
            name="operator action_id",
            maximum=128,
        )
        moment = _utc(occurred_at)

        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM astra_bybit_operator_state "
                        "WHERE singleton=TRUE FOR UPDATE"
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("Bybit operator state is not initialized")
                    current = _snapshot(row)
                    if current.mode is target and same_target_is_noop:
                        return current
                    if current.mode not in allowed_from:
                        raise RuntimeError(
                            f"operator transition {current.mode.value}->{target.value} "
                            "is not allowed"
                        )
                    next_generation = current.generation + 1
                    cursor.execute(
                        """INSERT INTO astra_bybit_operator_actions
                        (action_id, generation, from_mode, to_mode, actor, reason, occurred_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (
                            action_value,
                            next_generation,
                            current.mode.value,
                            target.value,
                            actor_value,
                            reason_value,
                            moment,
                        ),
                    )
                    cursor.execute(
                        """UPDATE astra_bybit_operator_state
                        SET mode=%s, generation=%s, updated_at=%s, updated_by=%s, reason=%s
                        WHERE singleton=TRUE AND generation=%s""",
                        (
                            target.value,
                            next_generation,
                            moment,
                            actor_value,
                            reason_value,
                            current.generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Bybit operator state generation changed concurrently")
                    cursor.execute(
                        "SELECT * FROM astra_bybit_operator_state WHERE singleton=TRUE"
                    )
                    updated = cursor.fetchone()
                    if updated is None:
                        raise RuntimeError("Bybit operator state disappeared after transition")
                    return _snapshot(updated)


def _snapshot(row: dict[str, object]) -> BybitOperatorSnapshot:
    updated_at = row["updated_at"]
    if not isinstance(updated_at, datetime):
        updated_at = datetime.fromisoformat(str(updated_at))
    return BybitOperatorSnapshot(
        mode=BybitOperatorMode(str(row["mode"])),
        generation=int(row["generation"]),
        updated_at=_utc(updated_at),
        updated_by=str(row["updated_by"]),
        reason=str(row["reason"]),
    )


def _action(row: dict[str, object]) -> BybitOperatorAction:
    occurred_at = row["occurred_at"]
    if not isinstance(occurred_at, datetime):
        occurred_at = datetime.fromisoformat(str(occurred_at))
    return BybitOperatorAction(
        action_id=str(row["action_id"]),
        generation=int(row["generation"]),
        from_mode=BybitOperatorMode(str(row["from_mode"])),
        to_mode=BybitOperatorMode(str(row["to_mode"])),
        actor=str(row["actor"]),
        reason=str(row["reason"]),
        occurred_at=_utc(occurred_at),
    )


def _utc(value: datetime | None) -> datetime:
    moment = datetime.now(UTC) if value is None else value
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("operator timestamp must be timezone-aware")
    return moment.astimezone(UTC)


def _bounded_text(value: str, *, name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return normalized
