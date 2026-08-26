from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_LEASE_NAME = "CANONICAL_DEMO_TRADING_RUNTIME"
_CONFIRMATION_PHRASE = "RECOVER_BYBIT_DEMO_RUNTIME_LEASE"
_LEASE_RELATION = "astra_bybit_demo_runtime_lease_v119"
_CHECKPOINT_RELATION = "astra_bybit_demo_active_excursion_v119"
_CONTROL_RELATION = "astra_bybit_demo_control_event_v121"
_CONTROL_TRIGGER = "astra_bybit_demo_control_append_only_v121"
_RECOVERY_RELATION = "astra_bybit_demo_runtime_lease_recovery_v123"
_RECOVERY_TRIGGERS = (
    "astra_bybit_demo_runtime_lease_recovery_append_only_v123",
    "astra_bybit_demo_runtime_lease_recovery_no_truncate_v123",
)


class BybitDemoRuntimeLeaseRecoveryStatus(StrEnum):
    NO_LEASE_PRESENT = "NO_LEASE_PRESENT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    BLOCKED = "BLOCKED"
    RECOVERED = "RECOVERED"
    ALREADY_RECOVERED = "ALREADY_RECOVERED"


@dataclass(frozen=True)
class BybitDemoRuntimeLeaseRecoveryInspection:
    status: BybitDemoRuntimeLeaseRecoveryStatus
    reasons: tuple[str, ...]
    lease_present: bool
    lease_owner_sha256: str | None
    lease_created_time_ms: int | None
    explicit_operator_halt_present: bool
    latest_control_event_id: str | None
    active_checkpoint_present: bool
    active_checkpoint_entry_order_link_id_sha256: str | None
    recovery_schema_ready: bool
    automatic_recovery_allowed: bool = False
    automatic_stale_takeover_allowed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def recoverable(self) -> bool:
        return self.status is BybitDemoRuntimeLeaseRecoveryStatus.RECOVERY_REQUIRED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_INSPECTION_V1",
            "status": self.status.value,
            "reasons": list(self.reasons),
            "lease_present": self.lease_present,
            "lease_owner_sha256": self.lease_owner_sha256,
            "lease_created_time_ms": self.lease_created_time_ms,
            "explicit_operator_halt_present": self.explicit_operator_halt_present,
            "latest_control_event_id": self.latest_control_event_id,
            "active_checkpoint_present": self.active_checkpoint_present,
            "active_checkpoint_entry_order_link_id_sha256": (
                self.active_checkpoint_entry_order_link_id_sha256
            ),
            "recovery_schema_ready": self.recovery_schema_ready,
            "recoverable": self.recoverable,
            "automatic_recovery_allowed": self.automatic_recovery_allowed,
            "automatic_stale_takeover_allowed": self.automatic_stale_takeover_allowed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


@dataclass(frozen=True)
class BybitDemoRuntimeLeaseRecoveryReceipt:
    status: BybitDemoRuntimeLeaseRecoveryStatus
    recovery_id: str
    lease_owner_sha256: str
    control_event_id: str
    active_checkpoint_present: bool
    created_at: datetime
    idempotent_existing_recovery: bool
    immutable_audit: bool = True
    automatic_recovery_allowed: bool = False
    automatic_stale_takeover_allowed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_RECEIPT_V1",
            "status": self.status.value,
            "recovery_id": self.recovery_id,
            "lease_owner_sha256": self.lease_owner_sha256,
            "control_event_id": self.control_event_id,
            "active_checkpoint_present": self.active_checkpoint_present,
            "created_at": self.created_at.isoformat(),
            "idempotent_existing_recovery": self.idempotent_existing_recovery,
            "immutable_audit": self.immutable_audit,
            "automatic_recovery_allowed": self.automatic_recovery_allowed,
            "automatic_stale_takeover_allowed": self.automatic_stale_takeover_allowed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


class PostgresBybitDemoRuntimeLeaseRecovery:
    """Explicitly recover an orphaned v119 lease without time-based takeover.

    Inspection exposes only a SHA-256 fingerprint of the lease owner. Recovery requires an explicit
    v121 HALT event, the exact fingerprint, a human process-stop evidence note, and the confirmation
    phrase. The raw owner token never leaves the transaction. The immutable v123 audit insert and
    exact lease deletion commit atomically under table locks, so a crash cannot record a recovery
    without releasing the lease or release the lease without recording the audit.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_recovery_allowed = False
    automatic_stale_takeover_allowed = False
    immutable_records = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit Demo runtime lease recovery DSN is required")
        self._dsn = dsn

    def inspect(self) -> BybitDemoRuntimeLeaseRecoveryInspection:
        _require_postgres()
        with psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    schema_ready = _schema_ready(cursor)
                    if not schema_ready:
                        return _inspection(
                            status=BybitDemoRuntimeLeaseRecoveryStatus.BLOCKED,
                            reasons=("DEMO_RUNTIME_LEASE_RECOVERY_SCHEMA_NOT_READY",),
                            recovery_schema_ready=False,
                        )
                    control = _latest_control_event(cursor)
                    lease = _lease_row(cursor, lock=False)
                    checkpoint = _checkpoint_row(cursor)
        explicit_halt = _is_explicit_halt(control)
        if lease is None:
            status = BybitDemoRuntimeLeaseRecoveryStatus.NO_LEASE_PRESENT
            reasons: tuple[str, ...] = ()
        elif not explicit_halt:
            status = BybitDemoRuntimeLeaseRecoveryStatus.BLOCKED
            reasons = ("DEMO_RUNTIME_LEASE_RECOVERY_REQUIRES_EXPLICIT_OPERATOR_HALT",)
        else:
            status = BybitDemoRuntimeLeaseRecoveryStatus.RECOVERY_REQUIRED
            reasons = ("DEMO_RUNTIME_LEASE_REQUIRES_CONTROLLED_OPERATOR_RECOVERY",)
        return _inspection_from_rows(
            status=status,
            reasons=reasons,
            lease=lease,
            control=control,
            checkpoint=checkpoint,
            recovery_schema_ready=True,
        )

    def recover(
        self,
        *,
        expected_lease_owner_sha256: str,
        operator_id: str,
        reason: str,
        process_stop_evidence: str,
        confirmation_phrase: str,
        now: datetime | None = None,
    ) -> BybitDemoRuntimeLeaseRecoveryReceipt:
        _validate_sha256(expected_lease_owner_sha256, "expected lease owner fingerprint")
        _validate_text(operator_id, "operator_id", 128)
        _validate_text(reason, "reason", 1000)
        _validate_text(process_stop_evidence, "process_stop_evidence", 1000)
        if confirmation_phrase != _CONFIRMATION_PHRASE:
            raise ValueError("Bybit Demo runtime lease recovery confirmation phrase is invalid")
        created_at = _require_utc_now(now)
        _require_postgres()

        with psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    if not _schema_ready(cursor):
                        raise RuntimeError("Bybit Demo runtime lease recovery schema is not ready")

                    # Block concurrent control inserts and all runtime lease acquire/release while
                    # the explicit HALT, lease identity, audit insertion and deletion are proven.
                    cursor.execute(
                        "LOCK TABLE astra_bybit_demo_control_event_v121 "
                        "IN SHARE ROW EXCLUSIVE MODE"
                    )
                    cursor.execute(
                        "LOCK TABLE astra_bybit_demo_runtime_lease_v119 "
                        "IN ACCESS EXCLUSIVE MODE"
                    )

                    control = _latest_control_event(cursor)
                    if not _is_explicit_halt(control):
                        raise RuntimeError(
                            "Bybit Demo runtime lease recovery requires latest explicit HALT"
                        )

                    lease = _lease_row(cursor, lock=True)
                    if lease is None:
                        existing = _recovery_by_owner_sha(
                            cursor,
                            expected_lease_owner_sha256,
                        )
                        if existing is None:
                            raise FileNotFoundError("Bybit Demo runtime lease does not exist")
                        return _receipt_from_row(existing, idempotent=True)

                    _validate_lease_safety(lease)
                    owner_token = lease["owner_token"]
                    owner_sha = _sha256_text(owner_token)
                    if owner_sha != expected_lease_owner_sha256:
                        raise RuntimeError(
                            "Bybit Demo runtime lease owner fingerprint changed before recovery"
                        )

                    checkpoint = _checkpoint_row(cursor)
                    checkpoint_hash = (
                        None
                        if checkpoint is None
                        else _sha256_text(checkpoint["entry_order_link_id"])
                    )
                    control_event_id = control["event_id"]
                    recovery_record = {
                        "lease_name": _LEASE_NAME,
                        "lease_owner_sha256": owner_sha,
                        "lease_created_time_ms": int(lease["created_time_ms"]),
                        "lease_process_id": int(lease["process_id"]),
                        "operator_id": operator_id.strip(),
                        "reason": reason.strip(),
                        "process_stop_evidence": process_stop_evidence.strip(),
                        "control_event_id": control_event_id,
                        "control_event_kind": "HALT_NEW_ENTRIES",
                        "active_checkpoint_present": checkpoint is not None,
                        "active_checkpoint_entry_order_link_id_sha256": checkpoint_hash,
                        "created_at": created_at.isoformat(),
                    }
                    recovery_id = _sha256_json(recovery_record)
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_runtime_lease_recovery_v123(
                               recovery_id,
                               lease_name,
                               lease_owner_sha256,
                               lease_created_time_ms,
                               lease_process_id,
                               operator_id,
                               reason,
                               process_stop_evidence,
                               control_event_id,
                               control_event_kind,
                               active_checkpoint_present,
                               active_checkpoint_entry_order_link_id_sha256,
                               created_at,
                               immutable_record,
                               order_writes_supported,
                               automatic_stale_takeover_allowed,
                               live_mainnet_order_routing_allowed
                           ) VALUES (
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, 'HALT_NEW_ENTRIES',
                               %s, %s, %s, true, false, false, false
                           )""",
                        (
                            recovery_id,
                            _LEASE_NAME,
                            owner_sha,
                            int(lease["created_time_ms"]),
                            int(lease["process_id"]),
                            operator_id.strip(),
                            reason.strip(),
                            process_stop_evidence.strip(),
                            control_event_id,
                            checkpoint is not None,
                            checkpoint_hash,
                            created_at,
                        ),
                    )
                    cursor.execute(
                        """DELETE FROM astra_bybit_demo_runtime_lease_v119
                           WHERE lease_name=%s AND owner_token=%s
                           RETURNING owner_token""",
                        (_LEASE_NAME, owner_token),
                    )
                    deleted = cursor.fetchone()
                    if deleted is None or deleted["owner_token"] != owner_token:
                        raise RuntimeError(
                            "Bybit Demo runtime lease changed during controlled recovery"
                        )

        return BybitDemoRuntimeLeaseRecoveryReceipt(
            status=BybitDemoRuntimeLeaseRecoveryStatus.RECOVERED,
            recovery_id=recovery_id,
            lease_owner_sha256=owner_sha,
            control_event_id=control_event_id,
            active_checkpoint_present=checkpoint is not None,
            created_at=created_at,
            idempotent_existing_recovery=False,
        )


def _schema_ready(cursor: Any) -> bool:
    for relation in (
        _LEASE_RELATION,
        _CHECKPOINT_RELATION,
        _CONTROL_RELATION,
        _RECOVERY_RELATION,
    ):
        cursor.execute("SELECT to_regclass(%s) AS relation", (relation,))
        row = cursor.fetchone()
        if row is None or row["relation"] is None:
            return False
    for trigger in (_CONTROL_TRIGGER, *_RECOVERY_TRIGGERS):
        cursor.execute(
            """SELECT count(*) AS count
               FROM pg_trigger
               WHERE NOT tgisinternal AND tgname=%s""",
            (trigger,),
        )
        row = cursor.fetchone()
        if row is None or int(row["count"]) != 1:
            return False
    return True


def _latest_control_event(cursor: Any) -> Any | None:
    cursor.execute(
        """SELECT event_id, event_kind, created_at
           FROM astra_bybit_demo_control_event_v121
           ORDER BY event_seq DESC
           LIMIT 1"""
    )
    return cursor.fetchone()


def _lease_row(cursor: Any, *, lock: bool) -> Any | None:
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        """SELECT owner_token, created_time_ms, process_id,
                  automatic_stale_takeover_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_runtime_lease_v119
           WHERE lease_name=%s"""
        + suffix,
        (_LEASE_NAME,),
    )
    return cursor.fetchone()


def _checkpoint_row(cursor: Any) -> Any | None:
    cursor.execute(
        """SELECT entry_order_link_id
           FROM astra_bybit_demo_active_excursion_v119
           WHERE checkpoint_name='ACTIVE'"""
    )
    return cursor.fetchone()


def _recovery_by_owner_sha(cursor: Any, owner_sha: str) -> Any | None:
    cursor.execute(
        """SELECT recovery_id, lease_owner_sha256, control_event_id,
                  active_checkpoint_present, created_at,
                  immutable_record, order_writes_supported,
                  automatic_stale_takeover_allowed,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_runtime_lease_recovery_v123
           WHERE lease_owner_sha256=%s""",
        (owner_sha,),
    )
    return cursor.fetchone()


def _receipt_from_row(row: Any, *, idempotent: bool) -> BybitDemoRuntimeLeaseRecoveryReceipt:
    if row["immutable_record"] is not True:
        raise ValueError("Bybit Demo runtime lease recovery audit lost immutable marker")
    if row["order_writes_supported"] is not False:
        raise ValueError("Bybit Demo runtime lease recovery audit cannot support order writes")
    if row["automatic_stale_takeover_allowed"] is not False:
        raise ValueError("Bybit Demo runtime lease recovery audit cannot allow stale takeover")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("Bybit Demo runtime lease recovery audit cannot permit mainnet routing")
    _validate_sha256(row["recovery_id"], "stored recovery id")
    _validate_sha256(row["lease_owner_sha256"], "stored lease owner fingerprint")
    _validate_sha256(row["control_event_id"], "stored control event id")
    created_at = row["created_at"]
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("Bybit Demo runtime lease recovery audit time is invalid")
    return BybitDemoRuntimeLeaseRecoveryReceipt(
        status=BybitDemoRuntimeLeaseRecoveryStatus.ALREADY_RECOVERED,
        recovery_id=row["recovery_id"],
        lease_owner_sha256=row["lease_owner_sha256"],
        control_event_id=row["control_event_id"],
        active_checkpoint_present=row["active_checkpoint_present"] is True,
        created_at=created_at.astimezone(UTC),
        idempotent_existing_recovery=idempotent,
    )


def _inspection_from_rows(
    *,
    status: BybitDemoRuntimeLeaseRecoveryStatus,
    reasons: tuple[str, ...],
    lease: Any | None,
    control: Any | None,
    checkpoint: Any | None,
    recovery_schema_ready: bool,
) -> BybitDemoRuntimeLeaseRecoveryInspection:
    lease_owner_sha = None
    lease_created_time_ms = None
    if lease is not None:
        _validate_lease_safety(lease)
        lease_owner_sha = _sha256_text(lease["owner_token"])
        lease_created_time_ms = int(lease["created_time_ms"])
    control_event_id = None
    if control is not None and _is_sha256(control["event_id"]):
        control_event_id = control["event_id"]
    checkpoint_hash = (
        None
        if checkpoint is None
        else _sha256_text(checkpoint["entry_order_link_id"])
    )
    return BybitDemoRuntimeLeaseRecoveryInspection(
        status=status,
        reasons=reasons,
        lease_present=lease is not None,
        lease_owner_sha256=lease_owner_sha,
        lease_created_time_ms=lease_created_time_ms,
        explicit_operator_halt_present=_is_explicit_halt(control),
        latest_control_event_id=control_event_id,
        active_checkpoint_present=checkpoint is not None,
        active_checkpoint_entry_order_link_id_sha256=checkpoint_hash,
        recovery_schema_ready=recovery_schema_ready,
    )


def _inspection(
    *,
    status: BybitDemoRuntimeLeaseRecoveryStatus,
    reasons: tuple[str, ...],
    recovery_schema_ready: bool,
) -> BybitDemoRuntimeLeaseRecoveryInspection:
    return BybitDemoRuntimeLeaseRecoveryInspection(
        status=status,
        reasons=reasons,
        lease_present=False,
        lease_owner_sha256=None,
        lease_created_time_ms=None,
        explicit_operator_halt_present=False,
        latest_control_event_id=None,
        active_checkpoint_present=False,
        active_checkpoint_entry_order_link_id_sha256=None,
        recovery_schema_ready=recovery_schema_ready,
    )


def _is_explicit_halt(row: Any | None) -> bool:
    return (
        row is not None
        and row["event_kind"] == "HALT_NEW_ENTRIES"
        and _is_sha256(row["event_id"])
    )


def _validate_lease_safety(row: Any) -> None:
    if row["automatic_stale_takeover_allowed"] is not False:
        raise ValueError("Bybit Demo runtime lease cannot allow automatic stale takeover")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("Bybit Demo runtime lease cannot permit mainnet routing")
    owner = row["owner_token"]
    if not isinstance(owner, str) or len(owner) != 64 or any(
        character not in "0123456789abcdef" for character in owner
    ):
        raise ValueError("Bybit Demo runtime lease owner token is invalid")
    created = row["created_time_ms"]
    process_id = row["process_id"]
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        raise ValueError("Bybit Demo runtime lease created time is invalid")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        raise ValueError("Bybit Demo runtime lease process id is invalid")


def _require_postgres() -> None:
    if psycopg is None or dict_row is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")


def _require_utc_now(value: datetime | None) -> datetime:
    now = datetime.now(UTC) if value is None else value
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Bybit Demo runtime lease recovery time must be timezone-aware")
    return now.astimezone(UTC)


def _validate_text(value: str, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"Bybit Demo runtime lease recovery {field} is invalid")


def _validate_sha256(value: object, label: str) -> None:
    if not _is_sha256(value):
        raise ValueError(f"Bybit Demo runtime lease recovery {label} must be sha256 hex")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _sha256_text(canonical)


__all__ = [
    "BybitDemoRuntimeLeaseRecoveryInspection",
    "BybitDemoRuntimeLeaseRecoveryReceipt",
    "BybitDemoRuntimeLeaseRecoveryStatus",
    "PostgresBybitDemoRuntimeLeaseRecovery",
]
