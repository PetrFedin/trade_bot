from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_excursion_store import (
    BybitDemoExcursionCheckpoint,
    _decode_checkpoint,
    _encode_checkpoint,
)
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_demo_runtime_lease import BybitDemoRuntimeLease
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class PostgresBybitDemoRuntimeLeaseRecord(BybitDemoRuntimeLease):
    fencing_token: int = 0


Clock = Callable[[], datetime]


class PostgresBybitDemoRuntimeLease:
    """Distributed demo lease with fencing and reconciliation-gated stale recovery.

    Expiry invalidates future state writes but never transfers ownership automatically. An expired
    lease stays active until an independent startup reconciliation proves broker truth and an
    explicit recovery action retires the old fence. The next acquire increments the fencing token.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_stale_takeover_allowed = False

    def __init__(
        self,
        dsn: str,
        *,
        lease_name: str = "bybit-demo-runtime",
        ttl_seconds: int = 120,
        clock: Clock | None = None,
        process_id: int | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if not lease_name.strip():
            raise ValueError("lease_name is required")
        if not 10 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be within [10, 3600]")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use Bybit PostgreSQL runtime")
        self.dsn = dsn
        self.lease_name = lease_name.strip()
        self.ttl_seconds = ttl_seconds
        self._clock = (lambda: datetime.now(UTC)) if clock is None else clock
        self._process_id = os.getpid() if process_id is None else process_id
        if self._process_id <= 0:
            raise ValueError("process_id must be positive")
        self._current: PostgresBybitDemoRuntimeLeaseRecord | None = None

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/product/005_bybit_runtime_state.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def acquire(self) -> PostgresBybitDemoRuntimeLeaseRecord:
        if self._current is not None:
            raise RuntimeError("Bybit PostgreSQL runtime lease is already held by this process")
        now = self._now()
        owner_token = secrets.token_hex(32)
        owner_hash = _token_hash(owner_token)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM astra_bybit_runtime_leases "
                        "WHERE lease_name=%s FOR UPDATE",
                        (self.lease_name,),
                    )
                    row = cursor.fetchone()
                    if row is not None and bool(row["active"]):
                        raise FileExistsError("Bybit PostgreSQL runtime lease is already active")
                    fencing_token = 1 if row is None else int(row["fencing_token"]) + 1
                    if row is None:
                        cursor.execute(
                            """INSERT INTO astra_bybit_runtime_leases
                            (lease_name, owner_token_sha256, owner_process_id, fencing_token,
                             acquired_at, heartbeat_at, expires_at, active)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, true)""",
                            (
                                self.lease_name,
                                owner_hash,
                                self._process_id,
                                fencing_token,
                                now,
                                now,
                                expires_at,
                            ),
                        )
                    else:
                        cursor.execute(
                            """UPDATE astra_bybit_runtime_leases
                            SET owner_token_sha256=%s, owner_process_id=%s, fencing_token=%s,
                                acquired_at=%s, heartbeat_at=%s, expires_at=%s, active=true
                            WHERE lease_name=%s""",
                            (
                                owner_hash,
                                self._process_id,
                                fencing_token,
                                now,
                                now,
                                expires_at,
                                self.lease_name,
                            ),
                        )
                    _append_event(
                        cursor,
                        event_id=f"LEASE_ACQUIRED:{self.lease_name}:{fencing_token}",
                        lease_name=self.lease_name,
                        fencing_token=fencing_token,
                        event_type="LEASE_ACQUIRED",
                        payload={"process_id": self._process_id},
                        occurred_at=now,
                    )
        lease = PostgresBybitDemoRuntimeLeaseRecord(
            owner_token=owner_token,
            created_time_ms=int(now.timestamp() * 1000),
            process_id=self._process_id,
            fencing_token=fencing_token,
        )
        self._current = lease
        return lease

    def heartbeat(self, *, owner_token: str) -> PostgresBybitDemoRuntimeLeaseRecord:
        lease = self._owned_current(owner_token)
        now = self._now()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _assert_fence(
                        cursor,
                        lease_name=self.lease_name,
                        owner_token=owner_token,
                        fencing_token=lease.fencing_token,
                        now=now,
                    )
                    cursor.execute(
                        """UPDATE astra_bybit_runtime_leases
                        SET heartbeat_at=%s, expires_at=%s
                        WHERE lease_name=%s""",
                        (now, expires_at, self.lease_name),
                    )
        return lease

    def release(self, *, owner_token: str) -> None:
        lease = self._owned_current(owner_token)
        now = self._now()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _assert_fence(
                        cursor,
                        lease_name=self.lease_name,
                        owner_token=owner_token,
                        fencing_token=lease.fencing_token,
                        now=now,
                    )
                    cursor.execute(
                        """UPDATE astra_bybit_runtime_leases
                        SET active=false, heartbeat_at=%s, expires_at=%s
                        WHERE lease_name=%s""",
                        (now, now, self.lease_name),
                    )
                    _append_event(
                        cursor,
                        event_id=f"LEASE_RELEASED:{self.lease_name}:{lease.fencing_token}",
                        lease_name=self.lease_name,
                        fencing_token=lease.fencing_token,
                        event_type="LEASE_RELEASED",
                        payload={"process_id": self._process_id},
                        occurred_at=now,
                    )
        self._current = None

    def inspect(self) -> PostgresBybitDemoRuntimeLeaseRecord:
        """Return diagnostics without exposing the persisted owner-token hash or a usable token."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM astra_bybit_runtime_leases WHERE lease_name=%s",
                    (self.lease_name,),
                )
                row = cursor.fetchone()
        if row is None or not bool(row["active"]):
            raise FileNotFoundError(self.lease_name)
        acquired_at = _datetime(row["acquired_at"])
        return PostgresBybitDemoRuntimeLeaseRecord(
            owner_token="0" * 64,
            created_time_ms=int(acquired_at.timestamp() * 1000),
            process_id=int(row["owner_process_id"]),
            fencing_token=int(row["fencing_token"]),
        )

    def current_lease(self) -> PostgresBybitDemoRuntimeLeaseRecord | None:
        return self._current

    def recover_expired(
        self,
        *,
        expected_fencing_token: int,
        broker_reconciliation: BybitStartupReconciliationResult,
        operator_reason: str,
    ) -> None:
        _validate_recovery_reconciliation(broker_reconciliation)
        if expected_fencing_token <= 0:
            raise ValueError("expected_fencing_token must be positive")
        if not operator_reason.strip():
            raise ValueError("operator_reason is required")
        now = self._now()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM astra_bybit_runtime_leases "
                        "WHERE lease_name=%s FOR UPDATE",
                        (self.lease_name,),
                    )
                    row = cursor.fetchone()
                    if row is None or not bool(row["active"]):
                        raise FileNotFoundError(self.lease_name)
                    if int(row["fencing_token"]) != expected_fencing_token:
                        raise RuntimeError("runtime lease fencing token changed before recovery")
                    if _datetime(row["expires_at"]) > now:
                        raise RuntimeError("runtime lease is not expired")
                    cursor.execute(
                        """UPDATE astra_bybit_runtime_leases
                        SET active=false, heartbeat_at=%s, expires_at=%s
                        WHERE lease_name=%s""",
                        (now, now, self.lease_name),
                    )
                    _append_event(
                        cursor,
                        event_id=(
                            f"LEASE_RECOVERED:{self.lease_name}:{expected_fencing_token}"
                        ),
                        lease_name=self.lease_name,
                        fencing_token=expected_fencing_token,
                        event_type="LEASE_RECOVERED_AFTER_RECONCILIATION",
                        payload={
                            "operator_reason": operator_reason.strip(),
                            "reconciliation_status": broker_reconciliation.status.value,
                            "reconciliation_reasons": list(broker_reconciliation.reasons),
                        },
                        occurred_at=now,
                    )

    def _owned_current(self, owner_token: str) -> PostgresBybitDemoRuntimeLeaseRecord:
        _validate_owner_token(owner_token)
        lease = self._current
        if lease is None or lease.owner_token != owner_token:
            raise RuntimeError("Bybit PostgreSQL runtime lease ownership is not current")
        return lease

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime lease clock must return timezone-aware datetime")
        return value.astimezone(UTC)


class PostgresBybitDemoExcursionStore:
    """PostgreSQL active-trade checkpoint guarded by the current distributed lease fence."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        dsn: str,
        *,
        runtime_lease: PostgresBybitDemoRuntimeLease,
        clock: Clock | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if runtime_lease.live_mainnet_order_routing_allowed:
            raise ValueError("PostgreSQL excursion store rejected mainnet-capable lease")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use Bybit PostgreSQL runtime")
        self.dsn = dsn
        self.runtime_lease = runtime_lease
        self._clock = (lambda: datetime.now(UTC)) if clock is None else clock

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def load(self) -> BybitDemoExcursionCheckpoint:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM astra_bybit_trades WHERE lifecycle_state='ACTIVE'"
                )
                rows = cursor.fetchall()
        if not rows:
            raise FileNotFoundError("active Bybit PostgreSQL trade")
        if len(rows) != 1:
            raise RuntimeError("multiple active Bybit PostgreSQL trades")
        return _checkpoint_from_row(rows[0])

    def initialize(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
    ) -> BybitDemoExcursionCheckpoint:
        checkpoint, state_payload = _checkpoint_payload(entry_order_link_id, state)
        lease = self._require_current_lease()
        now = self._now()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _assert_fence(
                        cursor,
                        lease_name=self.runtime_lease.lease_name,
                        owner_token=lease.owner_token,
                        fencing_token=lease.fencing_token,
                        now=now,
                    )
                    cursor.execute(
                        "SELECT entry_order_link_id FROM astra_bybit_trades "
                        "WHERE lifecycle_state='ACTIVE' FOR UPDATE"
                    )
                    if cursor.fetchone() is not None:
                        raise FileExistsError("active Bybit PostgreSQL trade already exists")
                    cursor.execute(
                        """INSERT INTO astra_bybit_trades
                        (entry_order_link_id, symbol, side, entry_price, initial_quantity,
                         current_quantity, stop_fraction, state_payload, revision_sha256,
                         lifecycle_state, fencing_token, created_at, updated_at, closed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                                'ACTIVE', %s, %s, %s, NULL)""",
                        (
                            entry_order_link_id,
                            state.symbol,
                            state.side.value,
                            state.entry_price,
                            state.initial_quantity,
                            state.current_quantity,
                            state.stop_fraction,
                            json.dumps(state_payload, sort_keys=True),
                            checkpoint.revision,
                            lease.fencing_token,
                            now,
                            now,
                        ),
                    )
                    _append_event(
                        cursor,
                        event_id=f"TRADE_INITIALIZED:{entry_order_link_id}",
                        lease_name=self.runtime_lease.lease_name,
                        fencing_token=lease.fencing_token,
                        event_type="TRADE_INITIALIZED",
                        payload={"revision_sha256": checkpoint.revision},
                        occurred_at=now,
                        entry_order_link_id=entry_order_link_id,
                    )
        return checkpoint

    def save(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
        expected_revision: str,
    ) -> BybitDemoExcursionCheckpoint:
        checkpoint, state_payload = _checkpoint_payload(entry_order_link_id, state)
        lease = self._require_current_lease()
        now = self._now()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _assert_fence(
                        cursor,
                        lease_name=self.runtime_lease.lease_name,
                        owner_token=lease.owner_token,
                        fencing_token=lease.fencing_token,
                        now=now,
                    )
                    row = _load_active_trade_for_update(cursor)
                    if str(row["entry_order_link_id"]) != entry_order_link_id:
                        raise ValueError("Bybit PostgreSQL checkpoint orderLinkId mismatch")
                    if str(row["revision_sha256"]) != expected_revision:
                        raise RuntimeError("Bybit PostgreSQL checkpoint revision changed")
                    if checkpoint.revision == expected_revision:
                        return _checkpoint_from_row(row)
                    cursor.execute(
                        """UPDATE astra_bybit_trades
                        SET symbol=%s, side=%s, entry_price=%s, initial_quantity=%s,
                            current_quantity=%s, stop_fraction=%s, state_payload=%s::jsonb,
                            revision_sha256=%s, fencing_token=%s, updated_at=%s
                        WHERE entry_order_link_id=%s AND lifecycle_state='ACTIVE'""",
                        (
                            state.symbol,
                            state.side.value,
                            state.entry_price,
                            state.initial_quantity,
                            state.current_quantity,
                            state.stop_fraction,
                            json.dumps(state_payload, sort_keys=True),
                            checkpoint.revision,
                            lease.fencing_token,
                            now,
                            entry_order_link_id,
                        ),
                    )
                    _append_event(
                        cursor,
                        event_id=f"TRADE_SAVED:{entry_order_link_id}:{checkpoint.revision}",
                        lease_name=self.runtime_lease.lease_name,
                        fencing_token=lease.fencing_token,
                        event_type="TRADE_SAVED",
                        payload={
                            "previous_revision_sha256": expected_revision,
                            "revision_sha256": checkpoint.revision,
                        },
                        occurred_at=now,
                        entry_order_link_id=entry_order_link_id,
                    )
        return checkpoint

    def clear(self, *, expected_revision: str) -> None:
        lease = self._require_current_lease()
        now = self._now()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _assert_fence(
                        cursor,
                        lease_name=self.runtime_lease.lease_name,
                        owner_token=lease.owner_token,
                        fencing_token=lease.fencing_token,
                        now=now,
                    )
                    row = _load_active_trade_for_update(cursor)
                    if str(row["revision_sha256"]) != expected_revision:
                        raise RuntimeError(
                            "Bybit PostgreSQL checkpoint revision changed before clear"
                        )
                    entry_order_link_id = str(row["entry_order_link_id"])
                    cursor.execute(
                        """UPDATE astra_bybit_trades
                        SET lifecycle_state='CLOSED', fencing_token=%s, updated_at=%s, closed_at=%s
                        WHERE entry_order_link_id=%s AND lifecycle_state='ACTIVE'""",
                        (lease.fencing_token, now, now, entry_order_link_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "Bybit PostgreSQL active trade disappeared before clear"
                        )
                    _append_event(
                        cursor,
                        event_id=f"TRADE_CLOSED:{entry_order_link_id}:{expected_revision}",
                        lease_name=self.runtime_lease.lease_name,
                        fencing_token=lease.fencing_token,
                        event_type="TRADE_CLOSED",
                        payload={"revision_sha256": expected_revision},
                        occurred_at=now,
                        entry_order_link_id=entry_order_link_id,
                    )

    def _require_current_lease(self) -> PostgresBybitDemoRuntimeLeaseRecord:
        lease = self.runtime_lease.current_lease()
        if lease is None:
            raise RuntimeError(
                "Bybit PostgreSQL state mutation requires an acquired runtime lease"
            )
        return lease

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("excursion store clock must return timezone-aware datetime")
        return value.astimezone(UTC)


def _checkpoint_payload(
    entry_order_link_id: str,
    state: BybitDemoTradeExcursionState,
) -> tuple[BybitDemoExcursionCheckpoint, dict[str, Any]]:
    raw, revision = _encode_checkpoint(
        entry_order_link_id=entry_order_link_id,
        state=state,
    )
    envelope = json.loads(raw)
    state_payload = envelope.get("state")
    if not isinstance(state_payload, dict):
        raise ValueError("encoded Bybit checkpoint is missing state")
    checkpoint = _decode_checkpoint(raw)
    if checkpoint.revision != revision:
        raise ValueError("encoded Bybit checkpoint revision mismatch")
    return checkpoint, state_payload


def _checkpoint_from_row(row: dict[str, Any]) -> BybitDemoExcursionCheckpoint:
    state_payload = row["state_payload"]
    if not isinstance(state_payload, dict):
        state_payload = dict(state_payload)
    envelope = {
        "schema_version": 1,
        "kind": "BYBIT_DEMO_TRADE_EXCURSION",
        "demo_only": True,
        "diagnostics_only": True,
        "exit_threshold_retuning_allowed": False,
        "live_mainnet_order_routing_allowed": False,
        "revision_sha256": str(row["revision_sha256"]),
        "entry_order_link_id": str(row["entry_order_link_id"]),
        "state": state_payload,
    }
    return _decode_checkpoint(
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def _load_active_trade_for_update(cursor) -> dict[str, Any]:
    cursor.execute(
        "SELECT * FROM astra_bybit_trades WHERE lifecycle_state='ACTIVE' FOR UPDATE"
    )
    rows = cursor.fetchall()
    if not rows:
        raise FileNotFoundError("active Bybit PostgreSQL trade")
    if len(rows) != 1:
        raise RuntimeError("multiple active Bybit PostgreSQL trades")
    return rows[0]


def _assert_fence(
    cursor,
    *,
    lease_name: str,
    owner_token: str,
    fencing_token: int,
    now: datetime,
) -> None:
    _validate_owner_token(owner_token)
    cursor.execute(
        "SELECT * FROM astra_bybit_runtime_leases WHERE lease_name=%s FOR UPDATE",
        (lease_name,),
    )
    row = cursor.fetchone()
    if row is None or not bool(row["active"]):
        raise RuntimeError("Bybit PostgreSQL runtime lease is not active")
    if int(row["fencing_token"]) != fencing_token:
        raise RuntimeError("Bybit PostgreSQL runtime lease fencing token changed")
    if str(row["owner_token_sha256"]) != _token_hash(owner_token):
        raise RuntimeError("Bybit PostgreSQL runtime lease ownership changed")
    if _datetime(row["expires_at"]) <= now:
        raise RuntimeError("Bybit PostgreSQL runtime lease expired")


def _append_event(
    cursor,
    *,
    event_id: str,
    lease_name: str,
    fencing_token: int,
    event_type: str,
    payload: dict[str, object],
    occurred_at: datetime,
    entry_order_link_id: str | None = None,
) -> None:
    cursor.execute(
        """INSERT INTO astra_bybit_runtime_events
        (event_id, lease_name, fencing_token, entry_order_link_id,
         event_type, payload, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (event_id) DO NOTHING""",
        (
            event_id,
            lease_name,
            fencing_token,
            entry_order_link_id,
            event_type,
            json.dumps(payload, sort_keys=True),
            occurred_at,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Bybit PostgreSQL runtime event ID already exists")


def _validate_recovery_reconciliation(
    result: BybitStartupReconciliationResult,
) -> None:
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("expired lease recovery rejected mainnet-capable reconciliation")
    if not result.broker_truth_complete:
        raise ValueError("expired lease recovery requires complete broker truth")
    if result.status not in {
        BybitStartupReconciliationStatus.READY_FOR_ENTRY,
        BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED,
    }:
        raise ValueError("expired lease recovery requires a non-blocked reconciliation result")


def _token_hash(owner_token: str) -> str:
    _validate_owner_token(owner_token)
    return hashlib.sha256(owner_token.encode("ascii")).hexdigest()


def _validate_owner_token(owner_token: str) -> None:
    if len(owner_token) != 64 or any(
        character not in "0123456789abcdef" for character in owner_token
    ):
        raise ValueError("runtime lease owner token must be 32-byte hex")


def _datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("PostgreSQL runtime timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
