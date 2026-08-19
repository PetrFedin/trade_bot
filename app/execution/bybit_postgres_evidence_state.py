from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_entry_provenance_store import (
    BybitDemoEntryProvenanceReceipt,
    BybitDemoEntryProvenanceRecord,
    _decode_record as _decode_provenance_record,
    _encode_record as _encode_provenance_record,
    _provenance_from_payload,
    _validate_provenance,
)
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_session_risk_ledger import BybitDemoSessionRiskLedger
from app.execution.bybit_demo_session_risk_store import (
    BybitDemoSessionRiskLedgerCheckpoint,
    _decode_checkpoint as _decode_session_checkpoint,
    _encode_checkpoint as _encode_session_checkpoint,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
    _decode_record as _decode_terminal_record,
    _encode_record as _encode_terminal_record,
    _validate_identity,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class _PostgresStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use Bybit PostgreSQL stores")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)


class PostgresBybitDemoEntryProvenanceStore(_PostgresStore):
    """Immutable, outcome-free entry decision provenance in PostgreSQL."""

    immutable_records = True
    realized_pnl_storage_allowed = False

    def persist(
        self,
        provenance: BybitDemoEntryDecisionProvenance,
    ) -> BybitDemoEntryProvenanceReceipt:
        _validate_provenance(provenance)
        canonical, envelope, record_sha = _encode_provenance_record(provenance)
        created_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_entry_provenance
                        (entry_order_link_id, record_sha256, envelope_text, created_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            provenance.entry_order_link_id,
                            record_sha,
                            envelope,
                            created_at,
                        ),
                    )
                    inserted = cursor.rowcount == 1
                    if not inserted:
                        cursor.execute(
                            """SELECT record_sha256, envelope_text
                            FROM astra_bybit_entry_provenance
                            WHERE entry_order_link_id=%s""",
                            (provenance.entry_order_link_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("entry provenance conflict row disappeared")
                        _record, stored_canonical, stored_sha = _decode_provenance_record(
                            str(row["envelope_text"])
                        )
                        if stored_canonical != canonical or stored_sha != record_sha:
                            raise RuntimeError(
                                "entry provenance conflict for existing entry orderLinkId"
                            )
        return BybitDemoEntryProvenanceReceipt(
            entry_order_link_id=provenance.entry_order_link_id,
            record_sha256=record_sha,
            idempotent_existing_record=not inserted,
        )

    def load(self, *, entry_order_link_id: str) -> BybitDemoEntryProvenanceRecord:
        if not entry_order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("entry provenance requires ASTRA-DEMO orderLinkId")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT record_sha256, envelope_text
                    FROM astra_bybit_entry_provenance
                    WHERE entry_order_link_id=%s""",
                    (entry_order_link_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError(entry_order_link_id)
        record, _canonical, record_sha = _decode_provenance_record(
            str(row["envelope_text"])
        )
        if record.get("entry_order_link_id") != entry_order_link_id:
            raise ValueError("entry provenance record entry orderLinkId mismatch")
        if str(row["record_sha256"]) != record_sha:
            raise ValueError("entry provenance database checksum mismatch")
        provenance = _provenance_from_payload(record)
        _validate_provenance(provenance)
        return BybitDemoEntryProvenanceRecord(
            provenance=provenance,
            record_sha256=record_sha,
        )


class PostgresBybitDemoTerminalEvidenceStore(_PostgresStore):
    """Immutable fully reconciled terminal evidence in PostgreSQL."""

    immutable_records = True

    def persist(
        self,
        *,
        entry_order_link_id: str,
        checkpoint_revision: str,
        evidence: BybitDemoProfitPreservationEvidence,
    ) -> BybitDemoTerminalEvidenceReceipt:
        _validate_identity(entry_order_link_id, checkpoint_revision, evidence)
        canonical, envelope, record_sha = _encode_terminal_record(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            evidence=evidence,
        )
        created_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_terminal_evidence
                        (entry_order_link_id, checkpoint_revision, record_sha256,
                         envelope_text, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            entry_order_link_id,
                            checkpoint_revision,
                            record_sha,
                            envelope,
                            created_at,
                        ),
                    )
                    inserted = cursor.rowcount == 1
                    if not inserted:
                        cursor.execute(
                            """SELECT checkpoint_revision, record_sha256, envelope_text
                            FROM astra_bybit_terminal_evidence
                            WHERE entry_order_link_id=%s""",
                            (entry_order_link_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("terminal evidence conflict row disappeared")
                        stored_canonical, stored_sha = _decode_terminal_record(
                            str(row["envelope_text"])
                        )
                        if (
                            str(row["checkpoint_revision"]) != checkpoint_revision
                            or stored_canonical != canonical
                            or stored_sha != record_sha
                        ):
                            raise RuntimeError(
                                "terminal evidence conflict for existing entry orderLinkId"
                            )
        return BybitDemoTerminalEvidenceReceipt(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            record_sha256=record_sha,
            idempotent_existing_record=not inserted,
        )


class PostgresBybitDemoSessionRiskLedgerStore(_PostgresStore):
    """Optimistically concurrent session-risk checkpoint without silent reset."""

    _LEDGER_KEY = "default"

    def load_current(self) -> BybitDemoSessionRiskLedgerCheckpoint:
        """Load the authoritative opening equity from the ledger itself.

        Normal product runtime uses this method so an environment variable cannot silently reset or
        redefine session loss history. First-time creation remains a separate explicit bootstrap.
        """

        row = self._load_row()
        checkpoint = _decode_session_checkpoint(str(row["envelope_text"]))
        if checkpoint.revision != str(row["revision_sha256"]):
            raise ValueError("session-risk database checksum mismatch")
        stored_opening = Decimal(str(row["opening_equity_usdt"]))
        if checkpoint.ledger.opening_equity_usdt != stored_opening:
            raise ValueError("session-risk payload opening equity mismatch")
        checkpoint.validate()
        return checkpoint

    def load(
        self,
        *,
        expected_opening_equity_usdt: Decimal,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        _validate_opening_equity(expected_opening_equity_usdt)
        checkpoint = self.load_current()
        if checkpoint.ledger.opening_equity_usdt != expected_opening_equity_usdt:
            raise ValueError("session-risk opening equity mismatch")
        return checkpoint

    def initialize(
        self,
        ledger: BybitDemoSessionRiskLedger,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        ledger.validate()
        envelope, revision = _encode_session_checkpoint(ledger)
        now = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_session_risk_ledger
                        (ledger_key, opening_equity_usdt, revision_sha256,
                         envelope_text, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (ledger_key) DO NOTHING""",
                        (
                            self._LEDGER_KEY,
                            ledger.opening_equity_usdt,
                            revision,
                            envelope,
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise FileExistsError("session-risk ledger already exists")
        checkpoint = _decode_session_checkpoint(envelope)
        checkpoint.validate()
        return checkpoint

    def save(
        self,
        ledger: BybitDemoSessionRiskLedger,
        *,
        expected_revision: str,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        ledger.validate()
        _validate_revision(expected_revision)
        envelope, revision = _encode_session_checkpoint(ledger)
        now = datetime.now(UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT opening_equity_usdt, revision_sha256
                        FROM astra_bybit_session_risk_ledger
                        WHERE ledger_key=%s FOR UPDATE""",
                        (self._LEDGER_KEY,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise FileNotFoundError(self._LEDGER_KEY)
                    if Decimal(str(row["opening_equity_usdt"])) != ledger.opening_equity_usdt:
                        raise ValueError("session-risk opening equity changed")
                    if str(row["revision_sha256"]) != expected_revision:
                        raise RuntimeError("session-risk revision changed concurrently")
                    cursor.execute(
                        """UPDATE astra_bybit_session_risk_ledger
                        SET revision_sha256=%s, envelope_text=%s, updated_at=%s
                        WHERE ledger_key=%s AND revision_sha256=%s""",
                        (
                            revision,
                            envelope,
                            now,
                            self._LEDGER_KEY,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("session-risk revision changed before update")
        checkpoint = _decode_session_checkpoint(envelope)
        checkpoint.validate()
        return checkpoint

    def _load_row(self):
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT opening_equity_usdt, revision_sha256, envelope_text
                    FROM astra_bybit_session_risk_ledger WHERE ledger_key=%s""",
                    (self._LEDGER_KEY,),
                )
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError(self._LEDGER_KEY)
        return row


def _validate_opening_equity(value: Decimal) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError("session-risk opening equity must be positive and finite")


def _validate_revision(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("session-risk revision must be sha256 hex")
