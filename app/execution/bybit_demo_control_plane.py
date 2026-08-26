from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo import BybitDemoOrderRequest
from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightResult,
    BybitDemoConnectedPreflightStatus,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_CONTROL_RELATION = "astra_bybit_demo_control_event_v121"
_CONTROL_TRIGGERS = (
    "astra_bybit_demo_control_append_only_v121",
    "astra_bybit_demo_control_no_truncate_v123",
)
_READY_STATUS = "READY_FOR_MANUAL_OPERATOR_APPROVAL"
_MAX_ARM_TTL = timedelta(minutes=5)
_MAX_PREFLIGHT_AGE = timedelta(seconds=30)
_MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)


class BybitDemoControlMode(StrEnum):
    HALTED = "HALTED"
    ARMED_NEW_ENTRIES = "ARMED_NEW_ENTRIES"


@dataclass(frozen=True)
class BybitDemoControlDecision:
    mode: BybitDemoControlMode
    reasons: tuple[str, ...]
    new_entry_allowed: bool
    latest_event_id: str | None
    latest_event_kind: str | None
    armed_until: datetime | None
    immutable_audit: bool = True
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_CONTROL_DECISION_V1",
            "mode": self.mode.value,
            "reasons": list(self.reasons),
            "new_entry_allowed": self.new_entry_allowed,
            "latest_event_id": self.latest_event_id,
            "latest_event_kind": self.latest_event_kind,
            "armed_until": (
                None if self.armed_until is None else self.armed_until.isoformat()
            ),
            "immutable_audit": self.immutable_audit,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


@dataclass(frozen=True)
class BybitDemoControlEventReceipt:
    event_id: str
    event_kind: str
    created_at: datetime
    armed_until: datetime | None
    preflight_record_sha256: str | None
    immutable_record: bool = True
    order_submission_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_CONTROL_EVENT_RECEIPT_V1",
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "created_at": self.created_at.isoformat(),
            "armed_until": (
                None if self.armed_until is None else self.armed_until.isoformat()
            ),
            "preflight_record_sha256": self.preflight_record_sha256,
            "immutable_record": self.immutable_record,
            "order_submission_supported": self.order_submission_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


class PostgresBybitDemoControlPlane:
    """Append-only, cryptographically verified operator control for new Demo exposure."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit Demo control-plane PostgreSQL DSN is required")
        self._dsn = dsn

    def read_decision(self, *, now: datetime) -> BybitDemoControlDecision:
        observed_at = _require_aware_utc(now, "control decision time")
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
                    cursor.execute("SELECT to_regclass(%s) AS relation", (_CONTROL_RELATION,))
                    relation = cursor.fetchone()
                    if relation is None or relation["relation"] is None:
                        return _halted("DEMO_CONTROL_SCHEMA_NOT_READY")
                    cursor.execute(
                        """SELECT count(*) AS count
                           FROM pg_trigger
                           WHERE NOT tgisinternal AND tgname = ANY(%s)""",
                        (list(_CONTROL_TRIGGERS),),
                    )
                    triggers = cursor.fetchone()
                    if triggers is None or int(triggers["count"]) != len(_CONTROL_TRIGGERS):
                        return _halted("DEMO_CONTROL_APPEND_ONLY_TRIGGER_NOT_READY")
                    cursor.execute(
                        """SELECT event_id, event_kind, operator_id, reason,
                                  preflight_status, preflight_record_sha256,
                                  preflight_canonical_record, preflight_observed_at,
                                  armed_until, created_at
                           FROM astra_bybit_demo_control_event_v121
                           ORDER BY event_seq DESC
                           LIMIT 1"""
                    )
                    row = cursor.fetchone()
        if row is None:
            return _halted("DEMO_CONTROL_NO_EVENT_DEFAULT_HALT")
        return _decision_from_row(row, now=observed_at)

    def arm_new_entries(
        self,
        preflight: BybitDemoConnectedPreflightResult,
        *,
        operator_id: str,
        reason: str,
        now: datetime,
        preflight_observed_at: datetime,
        ttl_seconds: int = 120,
    ) -> BybitDemoControlEventReceipt:
        created_at = _require_aware_utc(now, "control ARM time")
        observed_at = _require_aware_utc(
            preflight_observed_at,
            "connected preflight observation time",
        )
        _validate_operator_text(operator_id, reason)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("Bybit Demo control ARM ttl_seconds must be an integer")
        ttl = timedelta(seconds=ttl_seconds)
        if ttl <= timedelta(0) or ttl > _MAX_ARM_TTL:
            raise ValueError("Bybit Demo control ARM TTL must be within (0, 300] seconds")
        age = created_at - observed_at
        if age < -_MAX_FUTURE_CLOCK_SKEW:
            raise ValueError("Bybit Demo connected preflight timestamp is in the future")
        if age > _MAX_PREFLIGHT_AGE:
            raise ValueError("Bybit Demo connected preflight is too old to ARM")
        if observed_at > created_at:
            observed_at = created_at
        _validate_arm_preflight(preflight)

        preflight_record = _canonical_json(preflight.to_payload())
        preflight_sha = _sha256_text(preflight_record)
        armed_until = created_at + ttl
        event = {
            "event_kind": "ARM_NEW_ENTRIES",
            "operator_id": operator_id.strip(),
            "reason": reason.strip(),
            "preflight_status": _READY_STATUS,
            "preflight_record_sha256": preflight_sha,
            "preflight_observed_at": observed_at.isoformat(),
            "armed_until": armed_until.isoformat(),
            "created_at": created_at.isoformat(),
        }
        event_id = _sha256_json(event)
        return self._insert_event(
            event_id=event_id,
            event_kind="ARM_NEW_ENTRIES",
            operator_id=operator_id.strip(),
            reason=reason.strip(),
            preflight_status=_READY_STATUS,
            preflight_record_sha256=preflight_sha,
            preflight_canonical_record=preflight_record,
            preflight_observed_at=observed_at,
            armed_until=armed_until,
            created_at=created_at,
            require_idle_runtime=True,
        )

    def halt_new_entries(
        self,
        *,
        operator_id: str,
        reason: str,
        now: datetime,
    ) -> BybitDemoControlEventReceipt:
        created_at = _require_aware_utc(now, "control HALT time")
        _validate_operator_text(operator_id, reason)
        event = {
            "event_kind": "HALT_NEW_ENTRIES",
            "operator_id": operator_id.strip(),
            "reason": reason.strip(),
            "created_at": created_at.isoformat(),
        }
        event_id = _sha256_json(event)
        return self._insert_event(
            event_id=event_id,
            event_kind="HALT_NEW_ENTRIES",
            operator_id=operator_id.strip(),
            reason=reason.strip(),
            preflight_status=None,
            preflight_record_sha256=None,
            preflight_canonical_record=None,
            preflight_observed_at=None,
            armed_until=None,
            created_at=created_at,
            require_idle_runtime=False,
        )

    def _insert_event(
        self,
        *,
        event_id: str,
        event_kind: str,
        operator_id: str,
        reason: str,
        preflight_status: str | None,
        preflight_record_sha256: str | None,
        preflight_canonical_record: str | None,
        preflight_observed_at: datetime | None,
        armed_until: datetime | None,
        created_at: datetime,
        require_idle_runtime: bool,
    ) -> BybitDemoControlEventReceipt:
        if psycopg is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        with psycopg.connect(self._dsn, autocommit=False) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT to_regclass(%s)", (_CONTROL_RELATION,))
                    relation = cursor.fetchone()
                    if relation is None or relation[0] is None:
                        raise RuntimeError("Bybit Demo control-plane v121 schema is not ready")
                    cursor.execute(
                        """SELECT count(*)
                           FROM pg_trigger
                           WHERE NOT tgisinternal AND tgname = ANY(%s)""",
                        (list(_CONTROL_TRIGGERS),),
                    )
                    triggers = cursor.fetchone()
                    if triggers is None or int(triggers[0]) != len(_CONTROL_TRIGGERS):
                        raise RuntimeError(
                            "Bybit Demo control-plane immutability guards are not ready"
                        )
                    if require_idle_runtime:
                        cursor.execute(
                            "LOCK TABLE astra_bybit_demo_runtime_lease_v119 IN SHARE MODE"
                        )
                        cursor.execute(
                            "SELECT count(*) FROM astra_bybit_demo_runtime_lease_v119"
                        )
                        lease_count = int(cursor.fetchone()[0])
                        cursor.execute(
                            """SELECT count(*)
                               FROM astra_bybit_demo_active_excursion_v119
                               WHERE checkpoint_name='ACTIVE'"""
                        )
                        checkpoint_count = int(cursor.fetchone()[0])
                        if lease_count or checkpoint_count:
                            raise RuntimeError(
                                "Bybit Demo control ARM requires idle canonical runtime"
                            )
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_control_event_v121(
                               event_id, event_kind, operator_id, reason,
                               preflight_status, preflight_record_sha256,
                               preflight_canonical_record, preflight_observed_at,
                               armed_until, created_at
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            event_id,
                            event_kind,
                            operator_id,
                            reason,
                            preflight_status,
                            preflight_record_sha256,
                            preflight_canonical_record,
                            preflight_observed_at,
                            armed_until,
                            created_at,
                        ),
                    )
        return BybitDemoControlEventReceipt(
            event_id=event_id,
            event_kind=event_kind,
            created_at=created_at,
            armed_until=armed_until,
            preflight_record_sha256=preflight_record_sha256,
        )


class ControlPlaneGuardedBybitDemoClient:
    """Recheck short-lived ARM immediately before non-reduce-only Demo entry mutation."""

    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        client: Any,
        control_plane: Any,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if getattr(client, "environment", None) != "BYBIT_DEMO":
            raise ValueError("Demo control guard requires BYBIT_DEMO client")
        if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError("Demo control guard rejected mainnet-capable client")
        _validate_control_plane(control_plane)
        self._client = client
        self._control_plane = control_plane
        self._now_provider = (
            (lambda: datetime.now(UTC)) if now_provider is None else now_provider
        )

    @property
    def protection_state_read_supported(self) -> bool:
        return getattr(self._client, "protection_state_read_supported", False) is True

    def place_market_order(self, request: BybitDemoOrderRequest):
        request.validate()
        if not request.reduce_only:
            decision = self._control_plane.read_decision(now=self._now_provider())
            _validate_control_decision(decision)
            if not decision.new_entry_allowed:
                reason = ",".join(decision.reasons) or "DEMO_CONTROL_NEW_ENTRY_HALTED"
                raise RuntimeError(f"Bybit Demo new entry halted by control plane: {reason}")
        return self._client.place_market_order(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _decision_from_row(row: Any, *, now: datetime) -> BybitDemoControlDecision:
    event_id = row["event_id"]
    event_kind = row["event_kind"]
    operator_id = row["operator_id"]
    reason = row["reason"]
    created_at = row["created_at"]
    if not _is_sha256(event_id):
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if not _stored_operator_text_valid(operator_id, reason):
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if not isinstance(created_at, datetime):
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    created_at = _require_aware_utc(created_at, "stored control event time")

    if event_kind == "HALT_NEW_ENTRIES":
        event = {
            "event_kind": "HALT_NEW_ENTRIES",
            "operator_id": operator_id,
            "reason": reason,
            "created_at": created_at.isoformat(),
        }
        if _sha256_json(event) != event_id:
            return _halted("DEMO_CONTROL_EVENT_HASH_MISMATCH")
        return _halted(
            "DEMO_CONTROL_OPERATOR_HALT",
            latest_event_id=event_id,
            latest_event_kind=event_kind,
        )

    if event_kind != "ARM_NEW_ENTRIES":
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if row["preflight_status"] != _READY_STATUS:
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    canonical = row["preflight_canonical_record"]
    if not isinstance(canonical, str) or not canonical:
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    preflight_sha = row["preflight_record_sha256"]
    if not _is_sha256(preflight_sha):
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if _sha256_text(canonical) != preflight_sha:
        return _halted("DEMO_CONTROL_PREFLIGHT_AUDIT_HASH_MISMATCH")

    observed_at = row["preflight_observed_at"]
    armed_until = row["armed_until"]
    if not all(isinstance(value, datetime) for value in (observed_at, armed_until)):
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    observed_at = _require_aware_utc(observed_at, "stored preflight observation time")
    armed_until = _require_aware_utc(armed_until, "stored control armed-until time")
    event = {
        "event_kind": "ARM_NEW_ENTRIES",
        "operator_id": operator_id,
        "reason": reason,
        "preflight_status": _READY_STATUS,
        "preflight_record_sha256": preflight_sha,
        "preflight_observed_at": observed_at.isoformat(),
        "armed_until": armed_until.isoformat(),
        "created_at": created_at.isoformat(),
    }
    if _sha256_json(event) != event_id:
        return _halted("DEMO_CONTROL_EVENT_HASH_MISMATCH")
    if created_at - observed_at > _MAX_PREFLIGHT_AGE:
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if observed_at > created_at or armed_until <= created_at:
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if armed_until - created_at > _MAX_ARM_TTL:
        return _halted("DEMO_CONTROL_EVENT_INVALID")
    if created_at - now > _MAX_FUTURE_CLOCK_SKEW:
        return _halted("DEMO_CONTROL_EVENT_CLOCK_INVALID")
    if now >= armed_until:
        return _halted(
            "DEMO_CONTROL_ARM_EXPIRED",
            latest_event_id=event_id,
            latest_event_kind=event_kind,
            armed_until=armed_until,
        )
    return BybitDemoControlDecision(
        mode=BybitDemoControlMode.ARMED_NEW_ENTRIES,
        reasons=(),
        new_entry_allowed=True,
        latest_event_id=event_id,
        latest_event_kind=event_kind,
        armed_until=armed_until,
    )


def _halted(
    reason: str,
    *,
    latest_event_id: str | None = None,
    latest_event_kind: str | None = None,
    armed_until: datetime | None = None,
) -> BybitDemoControlDecision:
    return BybitDemoControlDecision(
        mode=BybitDemoControlMode.HALTED,
        reasons=(reason,),
        new_entry_allowed=False,
        latest_event_id=latest_event_id,
        latest_event_kind=latest_event_kind,
        armed_until=armed_until,
    )


def _validate_arm_preflight(preflight: BybitDemoConnectedPreflightResult) -> None:
    if preflight.status is not BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL:
        raise ValueError("Bybit Demo control ARM requires clean connected preflight")
    if preflight.reasons:
        raise ValueError("Bybit Demo control ARM rejected preflight reasons")
    if not preflight.read_only_api_key_verified:
        raise ValueError("Bybit Demo control ARM requires verified read-only key")
    if not preflight.required_relations_present or not preflight.append_only_triggers_present:
        raise ValueError("Bybit Demo control ARM requires verified v119/v120 schema")
    if preflight.runtime_lease_present or preflight.active_checkpoint_present:
        raise ValueError("Bybit Demo control ARM requires idle durable runtime")
    if preflight.open_position_count or preflight.open_order_count:
        raise ValueError("Bybit Demo control ARM requires flat exchange state")
    if not preflight.positive_equity or not preflight.positive_available_balance:
        raise ValueError("Bybit Demo control ARM requires positive Demo capital")
    if not preflight.preflight_only or preflight.trade_actionable:
        raise ValueError("Bybit Demo control ARM rejected actionable preflight")
    if preflight.order_writes_supported or preflight.live_mainnet_order_routing_allowed:
        raise ValueError("Bybit Demo control ARM rejected unsafe preflight capability")


def _validate_control_plane(control_plane: Any) -> None:
    if getattr(control_plane, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("Demo control guard rejected mainnet-capable control plane")
    if getattr(control_plane, "order_writes_supported", True) is not False:
        raise ValueError("Demo control plane cannot write orders")
    if getattr(control_plane, "order_submission_supported", True) is not False:
        raise ValueError("Demo control plane cannot submit orders")
    if getattr(control_plane, "immutable_records", False) is not True:
        raise ValueError("Demo control plane must use immutable records")
    if not callable(getattr(control_plane, "read_decision", None)):
        raise ValueError("Demo control plane must expose read_decision")


def _validate_control_decision(decision: Any) -> None:
    if getattr(decision, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("Demo control decision enabled mainnet routing")
    if getattr(decision, "order_writes_supported", True) is not False:
        raise ValueError("Demo control decision cannot write orders")
    if not isinstance(getattr(decision, "new_entry_allowed", None), bool):
        raise ValueError("Demo control decision missing new-entry boolean")


def _validate_operator_text(operator_id: str, reason: str) -> None:
    if not operator_id.strip() or len(operator_id.strip()) > 128:
        raise ValueError("Bybit Demo control operator_id is invalid")
    if not reason.strip() or len(reason.strip()) > 1000:
        raise ValueError("Bybit Demo control reason is invalid")


def _stored_operator_text_valid(operator_id: object, reason: object) -> bool:
    return (
        isinstance(operator_id, str)
        and operator_id == operator_id.strip()
        and 0 < len(operator_id) <= 128
        and isinstance(reason, str)
        and reason == reason.strip()
        and 0 < len(reason) <= 1000
    )


def _require_aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Bybit Demo {label} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


__all__ = [
    "BybitDemoControlDecision",
    "BybitDemoControlEventReceipt",
    "BybitDemoControlMode",
    "ControlPlaneGuardedBybitDemoClient",
    "PostgresBybitDemoControlPlane",
]
