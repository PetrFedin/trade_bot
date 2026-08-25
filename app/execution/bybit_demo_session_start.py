from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightStatus,
    PostgresBybitDemoOperationalStateReader,
)
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlMode,
    PostgresBybitDemoControlPlane,
)
from app.execution.bybit_demo_fixed_egress import (
    BybitDemoFixedEgressPreflightAccountClient,
    require_fixed_egress_ready_for_arm,
    run_bybit_demo_fixed_egress_connected_preflight,
)
from app.execution.bybit_demo_postgres_bootstrap import (
    BybitDemoPostgresBootstrapStatus,
    verify_bybit_demo_postgres_schema,
)
from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_session_risk_ledger import (
    start_bybit_demo_session_risk_ledger,
)
from app.execution.bybit_demo_session_risk_store import _encode_checkpoint

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_CONFIRMATION_PHRASE = "INITIALIZE_BYBIT_DEMO_SESSION_RISK"
_SESSION_START_LOCK_KEY = 122001
_SESSION_NAME = "ACTIVE"
_REQUIRED_V122_TRIGGERS = (
    "astra_bybit_demo_session_risk_guard_v122",
    "astra_bybit_demo_session_risk_no_truncate_v122",
    "astra_bybit_demo_session_outcome_append_only_v122",
    "astra_bybit_demo_session_outcome_no_truncate_v122",
)
_REQUIRED_V123_TRIGGERS = (
    "astra_bybit_demo_session_start_append_only_v123",
    "astra_bybit_demo_session_start_no_truncate_v123",
)


class BybitDemoSessionStartStatus(StrEnum):
    NOT_INITIALIZED = "NOT_INITIALIZED"
    INITIALIZED = "INITIALIZED"
    INITIALIZED_NOW = "INITIALIZED_NOW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitDemoSessionStartResult:
    status: BybitDemoSessionStartStatus
    reasons: tuple[str, ...]
    session_initialized: bool
    worker_session_ready: bool
    ledger_revision_sha256: str | None
    outcome_count: int
    opening_equity_positive: bool
    preflight_record_sha256: str | None = None
    git_sha: str | None = None
    session_start_id: str | None = None
    fixed_egress_required: bool = True
    explicit_operator_action_required: bool = True
    automatic_reset_allowed: bool = False
    trading_credential_required: bool = False
    order_write_performed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        return self.status is not BybitDemoSessionStartStatus.BLOCKED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_SESSION_START_V1",
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "session_initialized": self.session_initialized,
            "worker_session_ready": self.worker_session_ready,
            "ledger_revision_sha256": self.ledger_revision_sha256,
            "outcome_count": self.outcome_count,
            "opening_equity_positive": self.opening_equity_positive,
            "preflight_record_sha256": self.preflight_record_sha256,
            "git_sha": self.git_sha,
            "session_start_id": self.session_start_id,
            "fixed_egress_required": self.fixed_egress_required,
            "explicit_operator_action_required": self.explicit_operator_action_required,
            "automatic_reset_allowed": self.automatic_reset_allowed,
            "trading_credential_required": self.trading_credential_required,
            "order_write_performed": self.order_write_performed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


@dataclass(frozen=True)
class _SessionMetadata:
    opening_equity_usdt: Decimal
    ledger_revision_sha256: str
    outcome_count: int
    created_at: datetime


@dataclass(frozen=True)
class _SessionStartAudit:
    session_start_id: str
    git_sha: str
    preflight_record_sha256: str
    initial_ledger_revision_sha256: str
    started_at: datetime


class PostgresBybitDemoSessionStartCoordinator:
    """One-time fixed-egress initializer for durable v122 risk + v123 provenance."""

    fixed_egress_required = True
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    trading_credential_required = False
    automatic_reset_allowed = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit Demo session-start PostgreSQL DSN is required")
        self._dsn = dsn

    def read_status(self) -> BybitDemoSessionStartResult:
        bootstrap = verify_bybit_demo_postgres_schema(self._dsn)
        if bootstrap.status is not BybitDemoPostgresBootstrapStatus.VERIFIED_READY:
            return _blocked("DEMO_SESSION_SCHEMA_NOT_READY")
        metadata = self._read_metadata()
        if metadata is None:
            return BybitDemoSessionStartResult(
                status=BybitDemoSessionStartStatus.NOT_INITIALIZED,
                reasons=(),
                session_initialized=False,
                worker_session_ready=False,
                ledger_revision_sha256=None,
                outcome_count=0,
                opening_equity_positive=False,
            )
        checkpoint = PostgresBybitDemoSessionRiskLedgerStore(self._dsn).load(
            expected_opening_equity_usdt=metadata.opening_equity_usdt
        )
        if (
            checkpoint.revision != metadata.ledger_revision_sha256
            or len(checkpoint.ledger.outcomes) != metadata.outcome_count
        ):
            return _blocked_existing(
                "DEMO_SESSION_LEDGER_VERIFICATION_FAILED",
                metadata=metadata,
            )
        audit = self._read_start_audit()
        if audit is None:
            return _blocked_existing(
                "DEMO_SESSION_START_AUDIT_MISSING",
                metadata=metadata,
            )
        if audit.started_at != metadata.created_at:
            return _blocked_existing(
                "DEMO_SESSION_START_AUDIT_TIME_MISMATCH",
                metadata=metadata,
            )
        if (
            metadata.outcome_count == 0
            and audit.initial_ledger_revision_sha256 != checkpoint.revision
        ):
            return _blocked_existing(
                "DEMO_SESSION_START_AUDIT_REVISION_MISMATCH",
                metadata=metadata,
            )
        return BybitDemoSessionStartResult(
            status=BybitDemoSessionStartStatus.INITIALIZED,
            reasons=(),
            session_initialized=True,
            worker_session_ready=True,
            ledger_revision_sha256=checkpoint.revision,
            outcome_count=len(checkpoint.ledger.outcomes),
            opening_equity_positive=True,
            preflight_record_sha256=audit.preflight_record_sha256,
            git_sha=audit.git_sha,
            session_start_id=audit.session_start_id,
        )

    def initialize(
        self,
        account_client: BybitDemoFixedEgressPreflightAccountClient,
        *,
        confirmation_phrase: str,
        operator_id: str,
        reason: str,
        git_sha: str,
        now: datetime | None = None,
    ) -> BybitDemoSessionStartResult:
        if confirmation_phrase != _CONFIRMATION_PHRASE:
            raise ValueError("Bybit Demo session-start confirmation phrase is invalid")
        normalized_operator, normalized_reason = _validate_operator_text(operator_id, reason)
        validated_git_sha = _validate_git_sha(git_sha)
        observed_at = _aware_utc(datetime.now(UTC) if now is None else now)
        _validate_account_client(account_client)

        bootstrap = verify_bybit_demo_postgres_schema(self._dsn)
        if bootstrap.status is not BybitDemoPostgresBootstrapStatus.VERIFIED_READY:
            return _blocked("DEMO_SESSION_SCHEMA_NOT_READY", git_sha=validated_git_sha)
        existing = self.read_status()
        if existing.status is BybitDemoSessionStartStatus.INITIALIZED:
            return _existing_session_block(existing)
        if existing.status is BybitDemoSessionStartStatus.BLOCKED:
            return existing
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")

        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    locked = cursor.execute(
                        "SELECT pg_try_advisory_xact_lock(%s) AS locked",
                        (_SESSION_START_LOCK_KEY,),
                    ).fetchone()
                    if locked is None or locked["locked"] is not True:
                        return _blocked(
                            "DEMO_SESSION_START_LOCK_BUSY",
                            git_sha=validated_git_sha,
                        )
                    _lock_activation_tables(cursor)
                    _require_session_start_triggers(cursor)
                    if _count_runtime_leases(cursor):
                        return _blocked(
                            "DEMO_SESSION_RUNTIME_LEASE_PRESENT",
                            git_sha=validated_git_sha,
                        )
                    if _active_checkpoint_count(cursor):
                        return _blocked(
                            "DEMO_SESSION_ACTIVE_CHECKPOINT_PRESENT",
                            git_sha=validated_git_sha,
                        )
                    if _session_count(cursor):
                        current = self.read_status()
                        return _existing_session_block(current)

                    control = PostgresBybitDemoControlPlane(self._dsn).read_decision(
                        now=observed_at
                    )
                    if (
                        control.mode is not BybitDemoControlMode.HALTED
                        or control.new_entry_allowed
                    ):
                        return _blocked(
                            "DEMO_SESSION_CONTROL_NOT_HALTED",
                            git_sha=validated_git_sha,
                        )

                    preflight = run_bybit_demo_fixed_egress_connected_preflight(
                        account_client,
                        PostgresBybitDemoOperationalStateReader(self._dsn),
                    )
                    if (
                        preflight.status
                        is not BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
                    ):
                        return _blocked(
                            "DEMO_SESSION_CONNECTED_PREFLIGHT_NOT_READY",
                            git_sha=validated_git_sha,
                        )
                    require_fixed_egress_ready_for_arm(preflight)

                    wallet = account_client.get_wallet_balance()
                    wallet.validate()
                    if account_client.get_open_positions():
                        return _blocked(
                            "DEMO_SESSION_FINAL_POSITION_RECHECK_FAILED",
                            git_sha=validated_git_sha,
                        )
                    if account_client.get_open_orders():
                        return _blocked(
                            "DEMO_SESSION_FINAL_ORDER_RECHECK_FAILED",
                            git_sha=validated_git_sha,
                        )

                    ledger = start_bybit_demo_session_risk_ledger(
                        opening_equity_usdt=wallet.total_equity_usd
                    )
                    canonical_checkpoint, revision = _encode_checkpoint(ledger)
                    preflight_sha = _sha256_json(preflight.to_payload())
                    audit_canonical = _session_start_canonical_record(
                        operator_id=normalized_operator,
                        reason=normalized_reason,
                        git_sha=validated_git_sha,
                        preflight_record_sha256=preflight_sha,
                        initial_ledger_revision_sha256=revision,
                        started_at=observed_at,
                    )
                    session_start_id = hashlib.sha256(
                        audit_canonical.encode("utf-8")
                    ).hexdigest()
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_session_risk_v122(
                               session_name,
                               opening_equity_usdt,
                               peak_equity_usdt,
                               ledger_revision,
                               canonical_checkpoint,
                               outcome_count,
                               diagnostics_only,
                               order_writes_supported,
                               live_mainnet_order_routing_allowed,
                               created_at,
                               updated_at
                           ) VALUES (
                               %s, %s, %s, %s, %s, 0,
                               true, false, false, %s, %s
                           )""",
                        (
                            _SESSION_NAME,
                            ledger.opening_equity_usdt,
                            ledger.effective_peak_equity_usdt,
                            revision,
                            canonical_checkpoint,
                            observed_at,
                            observed_at,
                        ),
                    )
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_session_start_event_v123(
                               session_start_id,
                               session_name,
                               operator_id,
                               reason,
                               git_sha,
                               preflight_record_sha256,
                               initial_ledger_revision_sha256,
                               canonical_record,
                               immutable_record,
                               fixed_egress_required,
                               order_write_performed,
                               order_writes_supported,
                               live_mainnet_order_routing_allowed,
                               started_at,
                               created_at
                           ) VALUES (
                               %s, %s, %s, %s, %s, %s, %s, %s,
                               true, true, false, false, false, %s, %s
                           )""",
                        (
                            session_start_id,
                            _SESSION_NAME,
                            normalized_operator,
                            normalized_reason,
                            validated_git_sha,
                            preflight_sha,
                            revision,
                            audit_canonical,
                            observed_at,
                            observed_at,
                        ),
                    )

        verified = self.read_status()
        if verified.status is not BybitDemoSessionStartStatus.INITIALIZED:
            raise RuntimeError("Bybit Demo session-start persistence verification failed")
        if verified.session_start_id != session_start_id:
            raise RuntimeError("Bybit Demo session-start audit identity mismatch")
        return replace(verified, status=BybitDemoSessionStartStatus.INITIALIZED_NOW)

    def _read_metadata(self) -> _SessionMetadata | None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """SELECT opening_equity_usdt,
                                  ledger_revision,
                                  outcome_count,
                                  created_at
                           FROM astra_bybit_demo_session_risk_v122
                           WHERE session_name=%s""",
                        (_SESSION_NAME,),
                    )
                    row = cursor.fetchone()
        if row is None:
            return None
        revision = row["ledger_revision"]
        outcome_count = int(row["outcome_count"])
        created_at = _aware_utc(row["created_at"])
        if not _is_sha256(revision) or outcome_count < 0:
            raise ValueError("Bybit Demo session-start metadata is invalid")
        opening_equity = row["opening_equity_usdt"]
        if not isinstance(opening_equity, Decimal) or opening_equity <= 0:
            raise ValueError("Bybit Demo session-start opening equity metadata is invalid")
        return _SessionMetadata(
            opening_equity_usdt=opening_equity,
            ledger_revision_sha256=revision,
            outcome_count=outcome_count,
            created_at=created_at,
        )

    def _read_start_audit(self) -> _SessionStartAudit | None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """SELECT session_start_id,
                                  session_name,
                                  operator_id,
                                  reason,
                                  git_sha,
                                  preflight_record_sha256,
                                  initial_ledger_revision_sha256,
                                  canonical_record,
                                  immutable_record,
                                  fixed_egress_required,
                                  order_write_performed,
                                  order_writes_supported,
                                  live_mainnet_order_routing_allowed,
                                  started_at
                           FROM astra_bybit_demo_session_start_event_v123
                           WHERE session_name=%s""",
                        (_SESSION_NAME,),
                    )
                    row = cursor.fetchone()
        if row is None:
            return None
        _validate_audit_row(row)
        return _SessionStartAudit(
            session_start_id=row["session_start_id"],
            git_sha=row["git_sha"],
            preflight_record_sha256=row["preflight_record_sha256"],
            initial_ledger_revision_sha256=row["initial_ledger_revision_sha256"],
            started_at=_aware_utc(row["started_at"]),
        )


def _lock_activation_tables(cursor: Any) -> None:
    cursor.execute("LOCK TABLE astra_bybit_demo_runtime_lease_v119 IN SHARE MODE")
    cursor.execute("LOCK TABLE astra_bybit_demo_active_excursion_v119 IN SHARE MODE")
    cursor.execute("LOCK TABLE astra_bybit_demo_control_event_v121 IN SHARE MODE")
    cursor.execute(
        "LOCK TABLE astra_bybit_demo_session_risk_v122 IN SHARE ROW EXCLUSIVE MODE"
    )
    cursor.execute(
        "LOCK TABLE astra_bybit_demo_session_start_event_v123 IN SHARE ROW EXCLUSIVE MODE"
    )


def _require_session_start_triggers(cursor: Any) -> None:
    required = _REQUIRED_V122_TRIGGERS + _REQUIRED_V123_TRIGGERS
    cursor.execute(
        """SELECT count(*) AS count
           FROM pg_trigger
           WHERE NOT tgisinternal AND tgname = ANY(%s)""",
        (list(required),),
    )
    row = cursor.fetchone()
    if row is None or int(row["count"]) != len(required):
        raise RuntimeError("Bybit Demo session-start trigger contract is not ready")


def _count_runtime_leases(cursor: Any) -> int:
    cursor.execute("SELECT count(*) AS count FROM astra_bybit_demo_runtime_lease_v119")
    row = cursor.fetchone()
    return 0 if row is None else int(row["count"])


def _session_count(cursor: Any) -> int:
    cursor.execute("SELECT count(*) AS count FROM astra_bybit_demo_session_risk_v122")
    row = cursor.fetchone()
    return 0 if row is None else int(row["count"])


def _active_checkpoint_count(cursor: Any) -> int:
    cursor.execute(
        """SELECT count(*) AS count
           FROM astra_bybit_demo_active_excursion_v119
           WHERE checkpoint_name='ACTIVE'"""
    )
    row = cursor.fetchone()
    return 0 if row is None else int(row["count"])


def _validate_audit_row(row: Any) -> None:
    for field in (
        "session_start_id",
        "preflight_record_sha256",
        "initial_ledger_revision_sha256",
    ):
        if not _is_sha256(row[field]):
            raise ValueError("Bybit Demo session-start audit checksum metadata is invalid")
    git_sha = _validate_git_sha(row["git_sha"])
    operator_id, reason = _validate_operator_text(row["operator_id"], row["reason"])
    if row["session_name"] != _SESSION_NAME:
        raise ValueError("Bybit Demo session-start audit session identity is invalid")
    if row["immutable_record"] is not True or row["fixed_egress_required"] is not True:
        raise ValueError("Bybit Demo session-start audit safety markers are invalid")
    if row["order_write_performed"] is not False or row["order_writes_supported"] is not False:
        raise ValueError("Bybit Demo session-start audit cannot contain order writes")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("Bybit Demo session-start audit cannot permit mainnet routing")
    started_at = _aware_utc(row["started_at"])
    canonical = _session_start_canonical_record(
        operator_id=operator_id,
        reason=reason,
        git_sha=git_sha,
        preflight_record_sha256=row["preflight_record_sha256"],
        initial_ledger_revision_sha256=row["initial_ledger_revision_sha256"],
        started_at=started_at,
    )
    if row["canonical_record"] != canonical:
        raise ValueError("Bybit Demo session-start audit canonical record mismatch")
    calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if calculated != row["session_start_id"]:
        raise ValueError("Bybit Demo session-start audit SHA-256 mismatch")


def _session_start_canonical_record(
    *,
    operator_id: str,
    reason: str,
    git_sha: str,
    preflight_record_sha256: str,
    initial_ledger_revision_sha256: str,
    started_at: datetime,
) -> str:
    payload = {
        "schema": "BYBIT_DEMO_SESSION_START_AUDIT_V1",
        "session_name": _SESSION_NAME,
        "operator_id": operator_id,
        "reason": reason,
        "git_sha": git_sha,
        "preflight_record_sha256": preflight_record_sha256,
        "initial_ledger_revision_sha256": initial_ledger_revision_sha256,
        "fixed_egress_required": True,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "started_at": _aware_utc(started_at).isoformat(),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _existing_session_block(
    current: BybitDemoSessionStartResult,
) -> BybitDemoSessionStartResult:
    if current.status is not BybitDemoSessionStartStatus.INITIALIZED:
        raise RuntimeError("Bybit Demo existing session could not be verified")
    return replace(
        current,
        status=BybitDemoSessionStartStatus.BLOCKED,
        reasons=("DEMO_SESSION_ALREADY_INITIALIZED",),
    )


def _blocked_existing(
    reason: str,
    *,
    metadata: _SessionMetadata,
) -> BybitDemoSessionStartResult:
    return BybitDemoSessionStartResult(
        status=BybitDemoSessionStartStatus.BLOCKED,
        reasons=(reason,),
        session_initialized=True,
        worker_session_ready=False,
        ledger_revision_sha256=metadata.ledger_revision_sha256,
        outcome_count=metadata.outcome_count,
        opening_equity_positive=True,
    )


def _blocked(reason: str, *, git_sha: str | None = None) -> BybitDemoSessionStartResult:
    return BybitDemoSessionStartResult(
        status=BybitDemoSessionStartStatus.BLOCKED,
        reasons=(reason,),
        session_initialized=False,
        worker_session_ready=False,
        ledger_revision_sha256=None,
        outcome_count=0,
        opening_equity_positive=False,
        git_sha=git_sha,
    )


def _validate_account_client(client: Any) -> None:
    if getattr(client, "host", None) != "api-demo.bybit.com":
        raise ValueError("Bybit Demo session-start rejected non-demo host")
    if getattr(client, "fixed_egress_required", False) is not True:
        raise ValueError("Bybit Demo session-start requires fixed-egress account client")
    if getattr(client, "order_writes_supported", True) is not False:
        raise ValueError("Bybit Demo session-start account client must remain read-only")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("Bybit Demo session-start rejected mainnet-capable account client")
    for name in ("place_order", "place_market_order", "cancel_order", "amend_order"):
        if callable(getattr(client, name, None)):
            raise ValueError("Bybit Demo session-start account client exposes mutation method")


def _validate_operator_text(operator_id: str, reason: str) -> tuple[str, str]:
    normalized_operator = operator_id.strip()
    normalized_reason = reason.strip()
    if not normalized_operator or len(normalized_operator) > 128:
        raise ValueError("Bybit Demo session-start operator_id is invalid")
    if not normalized_reason or len(normalized_reason) > 1000:
        raise ValueError("Bybit Demo session-start reason is invalid")
    return normalized_operator, normalized_reason


def _validate_git_sha(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("Bybit Demo session-start git SHA must be 40-char hexadecimal")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Bybit Demo session-start time must be timezone-aware")
    return value.astimezone(UTC)


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


__all__ = [
    "BybitDemoSessionStartResult",
    "BybitDemoSessionStartStatus",
    "PostgresBybitDemoSessionStartCoordinator",
]
