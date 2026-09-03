from __future__ import annotations

from dataclasses import dataclass

from app.execution.bybit_demo_v120_persistence_records import (
    BybitDemoApprovedEntryAuthorizationV120,
    BybitDemoEntryDecisionProvenanceV120,
    BybitDemoTerminalEvidenceV120,
    canonical_sha256,
    decode_approved_entry_authorization_v120,
    decode_entry_provenance_v120,
    decode_terminal_evidence_v120,
    encode_approved_entry_authorization_v120,
    encode_entry_provenance_v120,
    encode_terminal_evidence_v120,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_APPROVAL_TABLE = "astra_bybit_demo_approved_entry_authorization_v120"
_PROVENANCE_TABLE = "astra_bybit_demo_entry_provenance_v120"
_TERMINAL_TABLE = "astra_bybit_demo_terminal_evidence_v120"


@dataclass(frozen=True)
class BybitDemoApprovedEntryAuthorizationReceiptV120:
    entry_order_link_id: str
    approval_id: str
    record_sha256: str
    idempotent_existing_record: bool
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoApprovedEntryAuthorizationStoredV120:
    authorization: BybitDemoApprovedEntryAuthorizationV120
    record_sha256: str
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoEntryProvenanceReceiptV120:
    entry_order_link_id: str
    record_sha256: str
    idempotent_existing_record: bool
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoEntryProvenanceStoredV120:
    provenance: BybitDemoEntryDecisionProvenanceV120
    record_sha256: str
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoTerminalEvidenceReceiptV120:
    entry_order_link_id: str
    checkpoint_revision: str
    record_sha256: str
    idempotent_existing_record: bool
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoTerminalEvidenceStoredV120:
    terminal: BybitDemoTerminalEvidenceV120
    record_sha256: str
    live_mainnet_order_routing_allowed: bool = False


class _PostgresV120Store:
    automatic_migration_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    live_mainnet_order_routing_allowed = False
    immutable_records = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("v120 persistence PostgreSQL DSN is required")
        self._dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)


class PostgresBybitDemoApprovedEntryAuthorizationStoreV120(_PostgresV120Store):
    """Immutable pre-submit authorization store; SELECT/INSERT only by contract."""

    outcome_storage_allowed = False
    realized_pnl_storage_allowed = False
    order_submission_supported = False

    def persist(
        self,
        authorization: BybitDemoApprovedEntryAuthorizationV120,
    ) -> BybitDemoApprovedEntryAuthorizationReceiptV120:
        canonical, record_sha = encode_approved_entry_authorization_v120(authorization)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""INSERT INTO {_APPROVAL_TABLE}
                        (entry_order_link_id, approval_id, source_snapshot_id,
                         source_evidence_rank, source_market_rank, record_sha256,
                         canonical_record, outcome_free, order_submission_supported,
                         realized_pnl_storage_allowed, live_mainnet_order_routing_allowed,
                         created_at)
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
                        return BybitDemoApprovedEntryAuthorizationReceiptV120(
                            entry_order_link_id=authorization.expected_entry_order_link_id,
                            approval_id=authorization.approval_id,
                            record_sha256=record_sha,
                            idempotent_existing_record=False,
                        )
                    row = _select_approval(cursor, authorization.expected_entry_order_link_id)
                    stored = _decode_approval_row(row)
                    if row["canonical_record"] != canonical:
                        raise RuntimeError(
                            "approved entry authorization conflict for existing entry orderLinkId"
                        )
                    if stored.record_sha256 != record_sha:
                        raise ValueError("approved entry authorization checksum mismatch")
                    if stored.authorization.approval_id != authorization.approval_id:
                        raise RuntimeError(
                            "approved entry authorization approval identity conflict"
                        )
                    return BybitDemoApprovedEntryAuthorizationReceiptV120(
                        entry_order_link_id=authorization.expected_entry_order_link_id,
                        approval_id=authorization.approval_id,
                        record_sha256=record_sha,
                        idempotent_existing_record=True,
                    )

    def load(
        self,
        *,
        entry_order_link_id: str,
    ) -> BybitDemoApprovedEntryAuthorizationStoredV120:
        _demo_order_link(entry_order_link_id, "approved entry authorization")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                row = _select_approval(cursor, entry_order_link_id)
        stored = _decode_approval_row(row)
        if stored.authorization.expected_entry_order_link_id != entry_order_link_id:
            raise ValueError("approved entry authorization orderLinkId mismatch")
        return stored


class PostgresBybitDemoEntryProvenanceStoreV120(_PostgresV120Store):
    """Immutable outcome-free protected-entry provenance; SELECT/INSERT only."""

    outcome_storage_allowed = False
    realized_pnl_storage_allowed = False

    def persist(
        self,
        provenance: BybitDemoEntryDecisionProvenanceV120,
    ) -> BybitDemoEntryProvenanceReceiptV120:
        canonical, record_sha = encode_entry_provenance_v120(provenance)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""INSERT INTO {_PROVENANCE_TABLE}
                        (entry_order_link_id, record_sha256, canonical_record,
                         outcome_free, realized_pnl_storage_allowed,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, true, false, false, now())
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (provenance.entry_order_link_id, record_sha, canonical),
                    )
                    if cursor.rowcount == 1:
                        return BybitDemoEntryProvenanceReceiptV120(
                            entry_order_link_id=provenance.entry_order_link_id,
                            record_sha256=record_sha,
                            idempotent_existing_record=False,
                        )
                    row = _select_provenance(cursor, provenance.entry_order_link_id)
                    stored = _decode_provenance_row(row)
                    if row["canonical_record"] != canonical:
                        raise RuntimeError(
                            "entry provenance conflict for existing entry orderLinkId"
                        )
                    if stored.record_sha256 != record_sha:
                        raise ValueError("entry provenance checksum mismatch")
                    return BybitDemoEntryProvenanceReceiptV120(
                        entry_order_link_id=provenance.entry_order_link_id,
                        record_sha256=record_sha,
                        idempotent_existing_record=True,
                    )

    def load(self, *, entry_order_link_id: str) -> BybitDemoEntryProvenanceStoredV120:
        _demo_order_link(entry_order_link_id, "entry provenance")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                row = _select_provenance(cursor, entry_order_link_id)
        stored = _decode_provenance_row(row)
        if stored.provenance.entry_order_link_id != entry_order_link_id:
            raise ValueError("entry provenance orderLinkId mismatch")
        return stored


class PostgresBybitDemoTerminalEvidenceStoreV120(_PostgresV120Store):
    """Immutable fully reconciled terminal diagnostics; SELECT/INSERT only."""

    terminal_outcome_storage_only = True
    entry_authorization_supported = False
    selector_retuning_supported = False

    def persist(
        self,
        terminal: BybitDemoTerminalEvidenceV120,
    ) -> BybitDemoTerminalEvidenceReceiptV120:
        canonical, record_sha = encode_terminal_evidence_v120(terminal)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""INSERT INTO {_TERMINAL_TABLE}
                        (entry_order_link_id, checkpoint_revision, record_sha256,
                         canonical_record, fully_reconciled_all_in, diagnostics_only,
                         exit_threshold_retuning_allowed,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, %s, true, true, false, false, now())
                        ON CONFLICT (entry_order_link_id) DO NOTHING""",
                        (
                            terminal.entry_order_link_id,
                            terminal.checkpoint_revision,
                            record_sha,
                            canonical,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return BybitDemoTerminalEvidenceReceiptV120(
                            entry_order_link_id=terminal.entry_order_link_id,
                            checkpoint_revision=terminal.checkpoint_revision,
                            record_sha256=record_sha,
                            idempotent_existing_record=False,
                        )
                    row = _select_terminal(cursor, terminal.entry_order_link_id)
                    stored = _decode_terminal_row(row)
                    if stored.terminal.checkpoint_revision != terminal.checkpoint_revision:
                        raise RuntimeError("terminal evidence checkpoint identity conflict")
                    if row["canonical_record"] != canonical:
                        raise RuntimeError(
                            "terminal evidence conflict for existing entry orderLinkId"
                        )
                    if stored.record_sha256 != record_sha:
                        raise ValueError("terminal evidence checksum mismatch")
                    return BybitDemoTerminalEvidenceReceiptV120(
                        entry_order_link_id=terminal.entry_order_link_id,
                        checkpoint_revision=terminal.checkpoint_revision,
                        record_sha256=record_sha,
                        idempotent_existing_record=True,
                    )

    def load(self, *, entry_order_link_id: str) -> BybitDemoTerminalEvidenceStoredV120:
        _demo_order_link(entry_order_link_id, "terminal evidence")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                row = _select_terminal(cursor, entry_order_link_id)
        stored = _decode_terminal_row(row)
        if stored.terminal.entry_order_link_id != entry_order_link_id:
            raise ValueError("terminal evidence orderLinkId mismatch")
        return stored


def _select_approval(cursor, entry_order_link_id: str):
    cursor.execute(
        f"""SELECT entry_order_link_id, approval_id, source_snapshot_id,
                  source_evidence_rank, source_market_rank, record_sha256,
                  canonical_record, outcome_free, order_submission_supported,
                  realized_pnl_storage_allowed, live_mainnet_order_routing_allowed
           FROM {_APPROVAL_TABLE}
           WHERE entry_order_link_id=%s""",
        (entry_order_link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("approved entry authorization does not exist")
    return row


def _select_provenance(cursor, entry_order_link_id: str):
    cursor.execute(
        f"""SELECT entry_order_link_id, record_sha256, canonical_record,
                  outcome_free, realized_pnl_storage_allowed,
                  live_mainnet_order_routing_allowed
           FROM {_PROVENANCE_TABLE}
           WHERE entry_order_link_id=%s""",
        (entry_order_link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("entry provenance record does not exist")
    return row


def _select_terminal(cursor, entry_order_link_id: str):
    cursor.execute(
        f"""SELECT entry_order_link_id, checkpoint_revision, record_sha256,
                  canonical_record, fully_reconciled_all_in, diagnostics_only,
                  exit_threshold_retuning_allowed, live_mainnet_order_routing_allowed
           FROM {_TERMINAL_TABLE}
           WHERE entry_order_link_id=%s""",
        (entry_order_link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("terminal evidence record does not exist")
    return row


def _decode_approval_row(row) -> BybitDemoApprovedEntryAuthorizationStoredV120:
    _validate_approval_row(row)
    canonical = row["canonical_record"]
    calculated = canonical_sha256(canonical)
    if calculated != row["record_sha256"]:
        raise ValueError("approved entry authorization checksum mismatch")
    authorization = decode_approved_entry_authorization_v120(canonical)
    if authorization.expected_entry_order_link_id != row["entry_order_link_id"]:
        raise ValueError("approved entry authorization stored orderLinkId mismatch")
    if authorization.approval_id != row["approval_id"]:
        raise ValueError("approved entry authorization stored approval id mismatch")
    if authorization.source_snapshot_id != row["source_snapshot_id"]:
        raise ValueError("approved entry authorization stored source snapshot mismatch")
    if authorization.source_evidence_rank != row["source_evidence_rank"]:
        raise ValueError("approved entry authorization stored evidence rank mismatch")
    if authorization.source_market_rank != row["source_market_rank"]:
        raise ValueError("approved entry authorization stored market rank mismatch")
    return BybitDemoApprovedEntryAuthorizationStoredV120(
        authorization=authorization,
        record_sha256=calculated,
    )


def _decode_provenance_row(row) -> BybitDemoEntryProvenanceStoredV120:
    _validate_provenance_row(row)
    canonical = row["canonical_record"]
    calculated = canonical_sha256(canonical)
    if calculated != row["record_sha256"]:
        raise ValueError("entry provenance checksum mismatch")
    provenance = decode_entry_provenance_v120(canonical)
    if provenance.entry_order_link_id != row["entry_order_link_id"]:
        raise ValueError("entry provenance stored orderLinkId mismatch")
    return BybitDemoEntryProvenanceStoredV120(
        provenance=provenance,
        record_sha256=calculated,
    )


def _decode_terminal_row(row) -> BybitDemoTerminalEvidenceStoredV120:
    _validate_terminal_row(row)
    canonical = row["canonical_record"]
    calculated = canonical_sha256(canonical)
    if calculated != row["record_sha256"]:
        raise ValueError("terminal evidence checksum mismatch")
    terminal = decode_terminal_evidence_v120(canonical)
    if terminal.entry_order_link_id != row["entry_order_link_id"]:
        raise ValueError("terminal evidence stored orderLinkId mismatch")
    if terminal.checkpoint_revision != row["checkpoint_revision"]:
        raise ValueError("terminal evidence stored checkpoint mismatch")
    return BybitDemoTerminalEvidenceStoredV120(
        terminal=terminal,
        record_sha256=calculated,
    )


def _validate_approval_row(row) -> None:
    if row["outcome_free"] is not True:
        raise ValueError("approved entry authorization lost outcome-free marker")
    if row["order_submission_supported"] is not False:
        raise ValueError("approved entry authorization store cannot submit orders")
    if row["realized_pnl_storage_allowed"] is not False:
        raise ValueError("approved entry authorization cannot store realized PnL")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("approved entry authorization cannot permit live routing")
    _canonical_row(row, "approved entry authorization")


def _validate_provenance_row(row) -> None:
    if row["outcome_free"] is not True:
        raise ValueError("entry provenance lost outcome-free marker")
    if row["realized_pnl_storage_allowed"] is not False:
        raise ValueError("entry provenance cannot store realized PnL")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("entry provenance cannot permit live routing")
    _canonical_row(row, "entry provenance")


def _validate_terminal_row(row) -> None:
    if row["fully_reconciled_all_in"] is not True:
        raise ValueError("terminal evidence must remain fully reconciled")
    if row["diagnostics_only"] is not True:
        raise ValueError("terminal evidence lost diagnostics-only marker")
    if row["exit_threshold_retuning_allowed"] is not False:
        raise ValueError("terminal evidence cannot authorize exit retuning")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("terminal evidence cannot permit live routing")
    _canonical_row(row, "terminal evidence")


def _canonical_row(row, label: str) -> None:
    canonical = row["canonical_record"]
    record_sha = row["record_sha256"]
    if not isinstance(canonical, str) or not canonical:
        raise ValueError(f"{label} canonical record is missing")
    if not isinstance(record_sha, str) or len(record_sha) != 64:
        raise ValueError(f"{label} checksum is invalid")


def _demo_order_link(value: str, label: str) -> None:
    if not value.startswith("ASTRA-DEMO-"):
        raise ValueError(f"{label} requires ASTRA-DEMO orderLinkId")


__all__ = [
    "BybitDemoApprovedEntryAuthorizationReceiptV120",
    "BybitDemoApprovedEntryAuthorizationStoredV120",
    "BybitDemoEntryProvenanceReceiptV120",
    "BybitDemoEntryProvenanceStoredV120",
    "BybitDemoTerminalEvidenceReceiptV120",
    "BybitDemoTerminalEvidenceStoredV120",
    "PostgresBybitDemoApprovedEntryAuthorizationStoreV120",
    "PostgresBybitDemoEntryProvenanceStoreV120",
    "PostgresBybitDemoTerminalEvidenceStoreV120",
]
