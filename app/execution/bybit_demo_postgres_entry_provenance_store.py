from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_entry_provenance_store import (
    BybitDemoEntryProvenanceReceipt,
    BybitDemoEntryProvenanceRecord,
    _encode_record,
    _provenance_from_payload,
    _validate_provenance,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresBybitDemoEntryProvenanceStore:
    """Immutable PostgreSQL outcome-free provenance for each protected Demo entry."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True
    realized_pnl_storage_allowed = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("entry provenance PostgreSQL DSN is required")
        self._dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def persist(
        self,
        provenance: BybitDemoEntryDecisionProvenance,
    ) -> BybitDemoEntryProvenanceReceipt:
        _validate_provenance(provenance)
        canonical, _envelope, record_sha = _encode_record(provenance)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_entry_provenance_v120
                        (entry_order_link_id, record_sha256, canonical_record,
                         outcome_free, realized_pnl_storage_allowed,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, true, false, false, now())
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            provenance.entry_order_link_id,
                            record_sha,
                            canonical,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return BybitDemoEntryProvenanceReceipt(
                            entry_order_link_id=provenance.entry_order_link_id,
                            record_sha256=record_sha,
                            idempotent_existing_record=False,
                        )
                    row = _select_provenance_row(cursor, provenance.entry_order_link_id)
                    _validate_stored_row(row)
                    if row["canonical_record"] != canonical:
                        raise RuntimeError(
                            "entry provenance conflict for existing entry orderLinkId"
                        )
                    if row["record_sha256"] != record_sha:
                        raise ValueError("entry provenance record checksum mismatch")
                    return BybitDemoEntryProvenanceReceipt(
                        entry_order_link_id=provenance.entry_order_link_id,
                        record_sha256=record_sha,
                        idempotent_existing_record=True,
                    )

    def load(self, *, entry_order_link_id: str) -> BybitDemoEntryProvenanceRecord:
        if not entry_order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("entry provenance requires ASTRA-DEMO orderLinkId")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                row = _select_provenance_row(cursor, entry_order_link_id)
        _validate_stored_row(row)
        canonical = row["canonical_record"]
        calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if calculated != row["record_sha256"]:
            raise ValueError("entry provenance record checksum mismatch")
        try:
            payload = json.loads(canonical)
        except json.JSONDecodeError as exc:
            raise ValueError("entry provenance canonical record is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("entry provenance canonical record must be an object")
        provenance = _provenance_from_payload(payload)
        _validate_provenance(provenance)
        if provenance.entry_order_link_id != entry_order_link_id:
            raise ValueError("entry provenance record entry orderLinkId mismatch")
        return BybitDemoEntryProvenanceRecord(
            provenance=provenance,
            record_sha256=calculated,
        )


def _select_provenance_row(cursor, entry_order_link_id: str):
    cursor.execute(
        """SELECT entry_order_link_id, record_sha256, canonical_record,
                  outcome_free, realized_pnl_storage_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_entry_provenance_v120
           WHERE entry_order_link_id=%s""",
        (entry_order_link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("entry provenance record does not exist")
    return row


def _validate_stored_row(row) -> None:
    if row["outcome_free"] is not True:
        raise ValueError("entry provenance record lost outcome-free marker")
    if row["realized_pnl_storage_allowed"] is not False:
        raise ValueError("entry provenance record cannot store realized PnL")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("entry provenance record cannot permit live routing")
    canonical = row["canonical_record"]
    record_sha = row["record_sha256"]
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("entry provenance canonical record is missing")
    if not isinstance(record_sha, str) or len(record_sha) != 64:
        raise ValueError("entry provenance record checksum is invalid")


__all__ = ["PostgresBybitDemoEntryProvenanceStore"]
