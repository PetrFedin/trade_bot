from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from app.execution.bybit_demo_runtime_lease import (
    BybitDemoRuntimeLease,
    validate_owner_token,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_LEASE_NAME = "CANONICAL_DEMO_TRADING_RUNTIME"
_V119_MIGRATION_PATH = Path("migrations/v119/001_bybit_demo_durable_runtime.sql")
_V119_MIGRATION_SHA256 = "c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e"
ClockMs = Callable[[], int]


class PostgresBybitDemoRuntimeLease:
    """Persistent fail-closed singleton lease for the canonical Demo runtime.

    The adapter has no broker client and no order methods. There is deliberately no TTL,
    heartbeat takeover, age-based recovery, or implicit lease stealing. An orphaned row remains
    blocking until a separately governed recovery capability verifies the prior runtime is gone.
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
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("demo runtime PostgreSQL DSN is required")
        self._dsn = dsn
        self._clock_ms = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = _V119_MIGRATION_PATH) -> None:
        migration_path = Path(path)
        payload = migration_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != _V119_MIGRATION_SHA256:
            raise ValueError(
                "v119 Demo runtime migration does not match the frozen canonical SHA-256"
            )
        sql = payload.decode("utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def acquire(self) -> BybitDemoRuntimeLease:
        created_time_ms = self._clock_ms()
        if (
            isinstance(created_time_ms, bool)
            or not isinstance(created_time_ms, int)
            or created_time_ms < 0
        ):
            raise ValueError("demo runtime lease clock must return a non-negative integer")

        lease = BybitDemoRuntimeLease(
            owner_token=secrets.token_hex(32),
            created_time_ms=created_time_ms,
            process_id=os.getpid(),
        )
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
                        (
                            _LEASE_NAME,
                            lease.owner_token,
                            lease.created_time_ms,
                            lease.process_id,
                        ),
                    )
                    if cursor.rowcount != 0:
                        return lease

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
                        raise RuntimeError(
                            "demo runtime lease conflict disappeared during acquire"
                        )
                    _lease_from_row(current)
                    raise FileExistsError("demo runtime lease already exists")

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
        validate_owner_token(owner_token)
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
    return BybitDemoRuntimeLease(
        owner_token=row["owner_token"],
        created_time_ms=row["created_time_ms"],
        process_id=row["process_id"],
        automatic_stale_takeover_allowed=row["automatic_stale_takeover_allowed"],
        live_mainnet_order_routing_allowed=row["live_mainnet_order_routing_allowed"],
        order_writes_supported=False,
    )


__all__ = ["PostgresBybitDemoRuntimeLease"]
