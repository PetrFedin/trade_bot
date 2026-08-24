from __future__ import annotations

from pathlib import Path

from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
    _encode_record,
    _validate_identity,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresBybitDemoTerminalEvidenceStore:
    """Immutable PostgreSQL fully reconciled terminal evidence for one Demo trade."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("terminal evidence PostgreSQL DSN is required")
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
        *,
        entry_order_link_id: str,
        checkpoint_revision: str,
        evidence: BybitDemoProfitPreservationEvidence,
    ) -> BybitDemoTerminalEvidenceReceipt:
        _validate_identity(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            evidence=evidence,
        )
        canonical, _envelope, record_sha = _encode_record(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            evidence=evidence,
        )
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_terminal_evidence_v120
                        (entry_order_link_id, checkpoint_revision, record_sha256,
                         canonical_record, fully_reconciled_all_in, diagnostics_only,
                         exit_threshold_retuning_allowed,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, %s, true, true, false, false, now())
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            entry_order_link_id,
                            checkpoint_revision,
                            record_sha,
                            canonical,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return BybitDemoTerminalEvidenceReceipt(
                            entry_order_link_id=entry_order_link_id,
                            checkpoint_revision=checkpoint_revision,
                            record_sha256=record_sha,
                            idempotent_existing_record=False,
                        )
                    row = _select_terminal_row(cursor, entry_order_link_id)
                    _validate_stored_row(row)
                    if row["checkpoint_revision"] != checkpoint_revision:
                        raise RuntimeError(
                            "terminal evidence conflict for existing entry orderLinkId"
                        )
                    if row["canonical_record"] != canonical:
                        raise RuntimeError(
                            "terminal evidence conflict for existing entry orderLinkId"
                        )
                    if row["record_sha256"] != record_sha:
                        raise ValueError("terminal evidence record checksum mismatch")
                    return BybitDemoTerminalEvidenceReceipt(
                        entry_order_link_id=entry_order_link_id,
                        checkpoint_revision=checkpoint_revision,
                        record_sha256=record_sha,
                        idempotent_existing_record=True,
                    )


def _select_terminal_row(cursor, entry_order_link_id: str):
    cursor.execute(
        """SELECT entry_order_link_id, checkpoint_revision, record_sha256,
                  canonical_record, fully_reconciled_all_in, diagnostics_only,
                  exit_threshold_retuning_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_terminal_evidence_v120
           WHERE entry_order_link_id=%s""",
        (entry_order_link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("terminal evidence record does not exist")
    return row


def _validate_stored_row(row) -> None:
    if row["fully_reconciled_all_in"] is not True:
        raise ValueError("terminal evidence must remain fully reconciled")
    if row["diagnostics_only"] is not True:
        raise ValueError("terminal evidence lost diagnostics-only marker")
    if row["exit_threshold_retuning_allowed"] is not False:
        raise ValueError("terminal evidence cannot authorize exit retuning")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("terminal evidence cannot permit live routing")
    canonical = row["canonical_record"]
    record_sha = row["record_sha256"]
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("terminal evidence canonical record is missing")
    if not isinstance(record_sha, str) or len(record_sha) != 64:
        raise ValueError("terminal evidence record checksum is invalid")


__all__ = ["PostgresBybitDemoTerminalEvidenceStore"]
