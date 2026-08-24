from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from app.execution.bybit_demo_runtime_lease import BybitDemoRuntimeLease

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_LEASE_NAME = "CANONICAL_DEMO_TRADING_RUNTIME"
ClockMs = Callable[[], int]


class PostgresBybitDemoRuntimeLease:
    """Persistent fail-closed singleton lease for the canonical Demo runtime.

    No TTL or automatic stale takeover exists. An orphaned row blocks new entries until an
    operator independently verifies that the prior runtime is not active and explicitly releases
    the exact owner token through the controlled recovery path.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_stale_takeover_allowed = False

    def __init__(
        self,
        dsn: str,
        *,
        clock_ms: ClockMs | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("demo runtime PostgreSQL DSN is required")
        self._dsn = dsn
        self._clock_ms = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v119/001_bybit_demo_durable_runtime.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def acquire(self) -> BybitDemoRuntimeLease:
        owner_token = secrets.token_hex(32)
        created_time_ms = self._clock_ms()
        if (
            isinstance(created_time_ms, bool)
            or not isinstance(created_time_ms, int)
            or created_time_ms < 0
        ):
            raise ValueError("demo runtime lease clock must return a non-negative integer")
        process_id = os.getpid()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_runtime_lease_v119
                        (lease_name, owner_token, created_time_ms, process_id,
                         automatic_stale_takeover_allowed,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, %s, false, false, now())
                        ON CONFLICT (lease_name) DO NOTHING""",
                        (_LEASE_NAME, owner_token, created_time_ms, process_id),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT owner_token, created_time_ms, process_id,
                                      automatic_stale_takeover_allowed,
                                      live_mainnet_order_routing_allowed
                               FROM astra_bybit_demo_runtime_lease_v119
                               WHERE lease_name=%s""",
                            (_LEASE_NAME,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError(
                                "demo runtime lease conflict disappeared during acquire"
                            )
                        _lease_from_row(row)
                        raise FileExistsError("demo runtime lease already exists")
        return BybitDemoRuntimeLease(
            owner_token=owner_token,
            created_time_ms=created_time_ms,
            process_id=process_id,
        )

    def inspect(self) -> BybitDemoRuntimeLease:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT owner_token, created_time_ms, process_id,
                              automatic_stale_takeover_allowed,
                              live_mainnet_order_routing_allowed
                       FROM astra_bybit_demo_runtime_lease_v119
                       WHERE lease_name=%s""",
                    (_LEASE_NAME,),
                )
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError("demo runtime lease does not exist")
        return _lease_from_row(row)

    def release(self, *, owner_token: str) -> None:
        _validate_owner_token(owner_token)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """DELETE FROM astra_bybit_demo_runtime_lease_v119
                           WHERE lease_name=%s AND owner_token=%s
                           RETURNING owner_token""",
                        (_LEASE_NAME, owner_token),
                    )
                    deleted = cursor.fetchone()
                    if deleted is not None:
                        return
                    cursor.execute(
                        """SELECT owner_token, created_time_ms, process_id,
                                  automatic_stale_takeover_allowed,
                                  live_mainnet_order_routing_allowed
                           FROM astra_bybit_demo_runtime_lease_v119
                           WHERE lease_name=%s""",
                        (_LEASE_NAME,),
                    )
                    current = cursor.fetchone()
                    if current is None:
                        raise RuntimeError("demo runtime lease disappeared before release")
                    _lease_from_row(current)
                    raise RuntimeError("demo runtime lease ownership changed before release")


def _lease_from_row(row) -> BybitDemoRuntimeLease:
    owner = row["owner_token"]
    created = row["created_time_ms"]
    process_id = row["process_id"]
    if row["automatic_stale_takeover_allowed"] is not False:
        raise ValueError("demo runtime PostgreSQL lease cannot allow automatic stale takeover")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("demo runtime PostgreSQL lease cannot permit live routing")
    _validate_owner_token(owner)
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        raise ValueError("demo runtime PostgreSQL lease has invalid created time")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        raise ValueError("demo runtime PostgreSQL lease has invalid process id")
    return BybitDemoRuntimeLease(
        owner_token=owner,
        created_time_ms=created,
        process_id=process_id,
    )


def _validate_owner_token(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("demo runtime lease owner token must be 32-byte hex")


__all__ = ["PostgresBybitDemoRuntimeLease"]
