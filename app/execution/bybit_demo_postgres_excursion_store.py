from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_excursion_store import (
    BybitDemoExcursionCheckpoint,
    _decode_state,
    _state_payload,
    _validate_identity,
    _validate_revision,
)
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_CHECKPOINT_NAME = "ACTIVE"


class PostgresBybitDemoExcursionStore:
    """Persistent singleton active-trade checkpoint with SHA-256 CAS revision."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("demo excursion PostgreSQL DSN is required")
        self._dsn = dsn

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

    def load(self) -> BybitDemoExcursionCheckpoint:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT entry_order_link_id, revision, state_json,
                              diagnostics_only, exit_threshold_retuning_allowed,
                              live_mainnet_order_routing_allowed
                       FROM astra_bybit_demo_active_excursion_v119
                       WHERE checkpoint_name=%s""",
                    (_CHECKPOINT_NAME,),
                )
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError("demo excursion checkpoint does not exist")
        return _checkpoint_from_row(row)

    def initialize(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
    ) -> BybitDemoExcursionCheckpoint:
        _validate_identity(entry_order_link_id, state)
        state_payload, revision = _encode_state_revision(entry_order_link_id, state)
        now = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_active_excursion_v119
                        (checkpoint_name, entry_order_link_id, revision, state_json,
                         diagnostics_only, exit_threshold_retuning_allowed,
                         live_mainnet_order_routing_allowed, created_at, updated_at)
                        VALUES (%s, %s, %s, %s::jsonb, true, false, false, %s, %s)
                        ON CONFLICT (checkpoint_name) DO NOTHING""",
                        (
                            _CHECKPOINT_NAME,
                            entry_order_link_id,
                            revision,
                            _canonical_json(state_payload),
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT entry_order_link_id, revision, state_json,
                                      diagnostics_only, exit_threshold_retuning_allowed,
                                      live_mainnet_order_routing_allowed
                               FROM astra_bybit_demo_active_excursion_v119
                               WHERE checkpoint_name=%s""",
                            (_CHECKPOINT_NAME,),
                        )
                        current = cursor.fetchone()
                        if current is None:
                            raise RuntimeError(
                                "demo excursion checkpoint conflict disappeared during initialize"
                            )
                        _checkpoint_from_row(current)
                        raise FileExistsError("demo excursion checkpoint already exists")
        return BybitDemoExcursionCheckpoint(
            entry_order_link_id=entry_order_link_id,
            state=state,
            revision=revision,
        )

    def save(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
        expected_revision: str,
    ) -> BybitDemoExcursionCheckpoint:
        _validate_identity(entry_order_link_id, state)
        _validate_revision(expected_revision)
        state_payload, revision = _encode_state_revision(entry_order_link_id, state)
        now = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE astra_bybit_demo_active_excursion_v119
                           SET revision=%s, state_json=%s::jsonb, updated_at=%s
                           WHERE checkpoint_name=%s
                             AND entry_order_link_id=%s
                             AND revision=%s
                           RETURNING entry_order_link_id""",
                        (
                            revision,
                            _canonical_json(state_payload),
                            now,
                            _CHECKPOINT_NAME,
                            entry_order_link_id,
                            expected_revision,
                        ),
                    )
                    updated = cursor.fetchone()
                    if updated is None:
                        _raise_checkpoint_cas_failure(
                            cursor,
                            entry_order_link_id=entry_order_link_id,
                            expected_revision=expected_revision,
                            operation="save",
                        )
        return BybitDemoExcursionCheckpoint(
            entry_order_link_id=entry_order_link_id,
            state=state,
            revision=revision,
        )

    def clear(self, *, expected_revision: str) -> None:
        _validate_revision(expected_revision)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """DELETE FROM astra_bybit_demo_active_excursion_v119
                           WHERE checkpoint_name=%s AND revision=%s
                           RETURNING entry_order_link_id""",
                        (_CHECKPOINT_NAME, expected_revision),
                    )
                    deleted = cursor.fetchone()
                    if deleted is None:
                        _raise_checkpoint_cas_failure(
                            cursor,
                            entry_order_link_id=None,
                            expected_revision=expected_revision,
                            operation="clear",
                        )


def _raise_checkpoint_cas_failure(
    cursor,
    *,
    entry_order_link_id: str | None,
    expected_revision: str,
    operation: str,
) -> None:
    cursor.execute(
        """SELECT entry_order_link_id, revision, state_json,
                  diagnostics_only, exit_threshold_retuning_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_active_excursion_v119
           WHERE checkpoint_name=%s""",
        (_CHECKPOINT_NAME,),
    )
    current = cursor.fetchone()
    if current is None:
        raise RuntimeError(f"demo excursion checkpoint disappeared before {operation}")
    checkpoint = _checkpoint_from_row(current)
    if entry_order_link_id is not None and checkpoint.entry_order_link_id != entry_order_link_id:
        raise ValueError("demo excursion checkpoint orderLinkId mismatch")
    if checkpoint.revision != expected_revision:
        suffix = "concurrently" if operation == "save" else "before clear"
        raise RuntimeError(f"demo excursion checkpoint revision changed {suffix}")
    raise RuntimeError(f"demo excursion checkpoint {operation} failed despite matching revision")


def _checkpoint_from_row(row) -> BybitDemoExcursionCheckpoint:
    if row["diagnostics_only"] is not True:
        raise ValueError("demo excursion PostgreSQL checkpoint lost diagnostics-only marker")
    if row["exit_threshold_retuning_allowed"] is not False:
        raise ValueError("demo excursion PostgreSQL checkpoint cannot authorize exit retuning")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("demo excursion PostgreSQL checkpoint cannot permit live routing")
    entry_order_link_id = row["entry_order_link_id"]
    revision = row["revision"]
    state_payload = row["state_json"]
    if (
        not isinstance(entry_order_link_id, str)
        or not entry_order_link_id.startswith("ASTRA-DEMO-")
    ):
        raise ValueError("demo excursion PostgreSQL checkpoint has invalid orderLinkId")
    _validate_revision(revision)
    if not isinstance(state_payload, dict):
        raise ValueError("demo excursion PostgreSQL checkpoint state must be an object")
    calculated = _revision_for_payload(entry_order_link_id, state_payload)
    if calculated != revision:
        raise ValueError("demo excursion PostgreSQL checkpoint checksum mismatch")
    state = _decode_state(state_payload)
    checkpoint = BybitDemoExcursionCheckpoint(
        entry_order_link_id=entry_order_link_id,
        state=state,
        revision=revision,
    )
    checkpoint.validate()
    return checkpoint


def _encode_state_revision(
    entry_order_link_id: str,
    state: BybitDemoTradeExcursionState,
) -> tuple[dict[str, Any], str]:
    payload = _state_payload(state)
    return payload, _revision_for_payload(entry_order_link_id, payload)


def _revision_for_payload(entry_order_link_id: str, state_payload: dict[str, Any]) -> str:
    canonical = _canonical_json(
        {
            "entry_order_link_id": entry_order_link_id,
            "state": state_payload,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


__all__ = ["PostgresBybitDemoExcursionStore"]
