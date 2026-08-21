from __future__ import annotations

from datetime import UTC, datetime

from app.execution.bybit_entry_recovery import (
    BybitEntryRecoveryEnvelope,
    BybitEntryRecoveryReceipt,
    BybitEntryRecoveryRecord,
    decode_entry_recovery_envelope,
    encode_entry_recovery_envelope,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresBybitEntryRecoveryStore:
    """Immutable pre-submit recovery facts keyed by deterministic Bybit orderLinkId."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use Bybit recovery state")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def persist(self, envelope: BybitEntryRecoveryEnvelope) -> BybitEntryRecoveryReceipt:
        canonical, record_sha = encode_entry_recovery_envelope(envelope)
        created_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_entry_recovery
                        (entry_order_link_id, record_sha256, envelope_text, created_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            envelope.entry_order_link_id,
                            record_sha,
                            canonical,
                            created_at,
                        ),
                    )
                    inserted = cursor.rowcount == 1
                    if not inserted:
                        cursor.execute(
                            """SELECT record_sha256, envelope_text
                            FROM astra_bybit_entry_recovery
                            WHERE entry_order_link_id=%s""",
                            (envelope.entry_order_link_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("entry recovery conflict row disappeared")
                        stored_sha = str(row["record_sha256"])
                        stored_text = str(row["envelope_text"])
                        decode_entry_recovery_envelope(
                            stored_text,
                            expected_sha256=stored_sha,
                        )
                        if stored_sha != record_sha or stored_text != canonical:
                            raise RuntimeError(
                                "entry recovery envelope conflict for existing orderLinkId"
                            )
        return BybitEntryRecoveryReceipt(
            entry_order_link_id=envelope.entry_order_link_id,
            record_sha256=record_sha,
            idempotent_existing_record=not inserted,
        )

    def load(self, *, entry_order_link_id: str) -> BybitEntryRecoveryRecord:
        if not entry_order_link_id.startswith("ASTRA-DEMO-E-"):
            raise ValueError("entry recovery load requires ASTRA-DEMO-E orderLinkId")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT record_sha256, envelope_text
                    FROM astra_bybit_entry_recovery
                    WHERE entry_order_link_id=%s""",
                    (entry_order_link_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError(entry_order_link_id)
        record = decode_entry_recovery_envelope(
            str(row["envelope_text"]),
            expected_sha256=str(row["record_sha256"]),
        )
        if record.envelope.entry_order_link_id != entry_order_link_id:
            raise ValueError("entry recovery database orderLinkId mismatch")
        return record
