from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.execution.bybit_demo_approval_lineage import (
    BybitDemoApprovedEntryAuthorization,
    validate_bybit_demo_approved_entry_authorization,
)
from app.execution.bybit_demo_approval_lineage_store import (
    BybitDemoApprovedEntryAuthorizationReceipt,
    BybitDemoApprovedEntryAuthorizationRecord,
    _authorization_from_payload,
    _encode_record,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresBybitDemoApprovedEntryAuthorizationStore:
    """Immutable PostgreSQL pre-submit approval lineage keyed by entry orderLinkId."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True
    outcome_storage_allowed = False
    realized_pnl_storage_allowed = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("approved entry authorization PostgreSQL DSN is required")
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
        authorization: BybitDemoApprovedEntryAuthorization,
    ) -> BybitDemoApprovedEntryAuthorizationReceipt:
        validate_bybit_demo_approved_entry_authorization(authorization)
        canonical, _envelope, record_sha = _encode_record(authorization)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_approved_entry_authorization_v120
                        (entry_order_link_id, approval_id, source_snapshot_id,
                         source_evidence_rank, source_market_rank, record_sha256,
                         canonical_record, outcome_free, order_submission_supported,
                         realized_pnl_storage_allowed,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s,
                                true, false, false, false, now())
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            authorization.expected_entry_order_link_id,
                            authorization.approval_id,
                            authorization.source_snapshot_id,
                            authorization.source_evidence_rank,
                            authorization.source_market_rank,
                            record_sha,
                            canonical,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return BybitDemoApprovedEntryAuthorizationReceipt(
                            entry_order_link_id=authorization.expected_entry_order_link_id,
                            approval_id=authorization.approval_id,
                            record_sha256=record_sha,
                            idempotent_existing_record=False,
                        )
                    row = _select_authorization_row(
                        cursor,
                        authorization.expected_entry_order_link_id,
                    )
                    _validate_stored_row(row)
                    if row["canonical_record"] != canonical:
                        raise RuntimeError(
                            "approved entry authorization conflict for existing entry orderLinkId"
                        )
                    if row["record_sha256"] != record_sha:
                        raise ValueError("approved entry authorization checksum mismatch")
                    if row["approval_id"] != authorization.approval_id:
                        raise RuntimeError(
                            "approved entry authorization approval identity conflict"
                        )
                    return BybitDemoApprovedEntryAuthorizationReceipt(
                        entry_order_link_id=authorization.expected_entry_order_link_id,
                        approval_id=authorization.approval_id,
                        record_sha256=record_sha,
                        idempotent_existing_record=True,
                    )

    def load(
        self,
        *,
        entry_order_link_id: str,
    ) -> BybitDemoApprovedEntryAuthorizationRecord:
        if not entry_order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("approved entry authorization requires ASTRA-DEMO orderLinkId")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                row = _select_authorization_row(cursor, entry_order_link_id)
        _validate_stored_row(row)
        canonical = row["canonical_record"]
        calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if calculated != row["record_sha256"]:
            raise ValueError("approved entry authorization checksum mismatch")
        try:
            payload = json.loads(canonical)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "approved entry authorization canonical record is invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("approved entry authorization canonical record must be an object")
        authorization = _authorization_from_payload(payload)
        if authorization.expected_entry_order_link_id != entry_order_link_id:
            raise ValueError("approved entry authorization orderLinkId mismatch")
        if authorization.approval_id != row["approval_id"]:
            raise ValueError("approved entry authorization approval id mismatch")
        return BybitDemoApprovedEntryAuthorizationRecord(
            authorization=authorization,
            record_sha256=calculated,
        )


def _select_authorization_row(cursor, entry_order_link_id: str):
    cursor.execute(
        """SELECT entry_order_link_id, approval_id, source_snapshot_id,
                  source_evidence_rank, source_market_rank, record_sha256,
                  canonical_record, outcome_free, order_submission_supported,
                  realized_pnl_storage_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_approved_entry_authorization_v120
           WHERE entry_order_link_id=%s""",
        (entry_order_link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("approved entry authorization does not exist")
    return row


def _validate_stored_row(row) -> None:
    if row["outcome_free"] is not True:
        raise ValueError("approved entry authorization lost outcome-free marker")
    if row["order_submission_supported"] is not False:
        raise ValueError("approved entry authorization store cannot submit orders")
    if row["realized_pnl_storage_allowed"] is not False:
        raise ValueError("approved entry authorization cannot store realized PnL")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("approved entry authorization cannot permit live routing")
    canonical = row["canonical_record"]
    record_sha = row["record_sha256"]
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("approved entry authorization canonical record is missing")
    if not isinstance(record_sha, str) or len(record_sha) != 64:
        raise ValueError("approved entry authorization checksum is invalid")


__all__ = ["PostgresBybitDemoApprovedEntryAuthorizationStore"]
