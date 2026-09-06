from __future__ import annotations

from app.execution.bybit_demo_v119_excursion_records import (
    BybitDemoExcursionCheckpointV119,
    BybitDemoExcursionStateV119,
    build_excursion_checkpoint_v119,
    canonical_json_v119,
    decode_excursion_state_v119,
    encode_excursion_state_v119,
    excursion_revision_v119,
    validate_demo_order_link_v119,
    validate_excursion_revision_v119,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_CHECKPOINT_NAME = "ACTIVE"


class PostgresBybitDemoExcursionStoreV119:
    """Strategy-free singleton active-excursion checkpoint with SHA-256 CAS."""

    automatic_migration_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    broker_network_supported = False
    market_data_reads_supported = False
    strategy_decisions_supported = False
    arm_halt_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("demo excursion PostgreSQL DSN is required")
        self._dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def load(self) -> BybitDemoExcursionCheckpointV119:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                row = _select_active(cursor)
        if row is None:
            raise FileNotFoundError("demo excursion checkpoint does not exist")
        return _checkpoint_from_row(row)

    def initialize(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoExcursionStateV119,
    ) -> BybitDemoExcursionCheckpointV119:
        checkpoint = build_excursion_checkpoint_v119(
            entry_order_link_id=entry_order_link_id,
            state=state,
        )
        payload = encode_excursion_state_v119(state)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_active_excursion_v119
                        (checkpoint_name, entry_order_link_id, revision, state_json,
                         diagnostics_only, exit_threshold_retuning_allowed,
                         live_mainnet_order_routing_allowed, created_at, updated_at)
                        VALUES (%s, %s, %s, %s::jsonb, true, false, false, now(), now())
                        ON CONFLICT (checkpoint_name) DO NOTHING""",
                        (
                            _CHECKPOINT_NAME,
                            entry_order_link_id,
                            checkpoint.revision,
                            canonical_json_v119(payload),
                        ),
                    )
                    if cursor.rowcount == 0:
                        current = _select_active(cursor)
                        if current is None:
                            raise RuntimeError(
                                "demo excursion checkpoint conflict disappeared during initialize"
                            )
                        _checkpoint_from_row(current)
                        raise FileExistsError("demo excursion checkpoint already exists")
        return checkpoint

    def save(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoExcursionStateV119,
        expected_revision: str,
    ) -> BybitDemoExcursionCheckpointV119:
        validate_demo_order_link_v119(entry_order_link_id)
        validate_excursion_revision_v119(expected_revision)
        checkpoint = build_excursion_checkpoint_v119(
            entry_order_link_id=entry_order_link_id,
            state=state,
        )
        payload = encode_excursion_state_v119(state)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE astra_bybit_demo_active_excursion_v119
                           SET revision=%s, state_json=%s::jsonb, updated_at=now()
                           WHERE checkpoint_name=%s
                             AND entry_order_link_id=%s
                             AND revision=%s
                           RETURNING entry_order_link_id""",
                        (
                            checkpoint.revision,
                            canonical_json_v119(payload),
                            _CHECKPOINT_NAME,
                            entry_order_link_id,
                            expected_revision,
                        ),
                    )
                    if cursor.fetchone() is None:
                        _raise_cas_failure(
                            cursor,
                            entry_order_link_id=entry_order_link_id,
                            expected_revision=expected_revision,
                            operation="save",
                        )
        return checkpoint

    def clear(self, *, expected_revision: str) -> None:
        validate_excursion_revision_v119(expected_revision)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """DELETE FROM astra_bybit_demo_active_excursion_v119
                           WHERE checkpoint_name=%s AND revision=%s
                           RETURNING entry_order_link_id""",
                        (_CHECKPOINT_NAME, expected_revision),
                    )
                    if cursor.fetchone() is None:
                        _raise_cas_failure(
                            cursor,
                            entry_order_link_id=None,
                            expected_revision=expected_revision,
                            operation="clear",
                        )


def _select_active(cursor):
    cursor.execute(
        """SELECT entry_order_link_id, revision, state_json,
                  diagnostics_only, exit_threshold_retuning_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_active_excursion_v119
           WHERE checkpoint_name=%s""",
        (_CHECKPOINT_NAME,),
    )
    return cursor.fetchone()


def _raise_cas_failure(
    cursor,
    *,
    entry_order_link_id: str | None,
    expected_revision: str,
    operation: str,
) -> None:
    current = _select_active(cursor)
    if current is None:
        raise RuntimeError(f"demo excursion checkpoint disappeared before {operation}")
    checkpoint = _checkpoint_from_row(current)
    if entry_order_link_id is not None and checkpoint.entry_order_link_id != entry_order_link_id:
        raise ValueError("demo excursion checkpoint orderLinkId mismatch")
    if checkpoint.revision != expected_revision:
        suffix = "concurrently" if operation == "save" else "before clear"
        raise RuntimeError(f"demo excursion checkpoint revision changed {suffix}")
    raise RuntimeError(f"demo excursion checkpoint {operation} failed despite matching revision")


def _checkpoint_from_row(row) -> BybitDemoExcursionCheckpointV119:
    if row["diagnostics_only"] is not True:
        raise ValueError("demo excursion PostgreSQL checkpoint lost diagnostics-only marker")
    if row["exit_threshold_retuning_allowed"] is not False:
        raise ValueError("demo excursion PostgreSQL checkpoint cannot authorize exit retuning")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("demo excursion PostgreSQL checkpoint cannot permit live routing")

    entry_order_link_id = row["entry_order_link_id"]
    revision = row["revision"]
    state_payload = row["state_json"]
    validate_demo_order_link_v119(entry_order_link_id)
    validate_excursion_revision_v119(revision)
    if not isinstance(state_payload, dict):
        raise ValueError("demo excursion PostgreSQL checkpoint state must be an object")
    state = decode_excursion_state_v119(state_payload)
    calculated = excursion_revision_v119(entry_order_link_id, state)
    if calculated != revision:
        raise ValueError("demo excursion PostgreSQL checkpoint checksum mismatch")
    checkpoint = BybitDemoExcursionCheckpointV119(
        entry_order_link_id=entry_order_link_id,
        state=state,
        revision=revision,
    )
    checkpoint.validate()
    return checkpoint


__all__ = ["PostgresBybitDemoExcursionStoreV119"]
