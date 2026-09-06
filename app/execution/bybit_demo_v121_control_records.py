from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

_READY_STATUS = "READY_FOR_MANUAL_OPERATOR_APPROVAL"
_MAX_ARM_TTL = timedelta(minutes=5)
_MAX_PREFLIGHT_AGE = timedelta(seconds=30)
_MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)


class BybitDemoControlEventKindV121(StrEnum):
    ARM_NEW_ENTRIES = "ARM_NEW_ENTRIES"
    HALT_NEW_ENTRIES = "HALT_NEW_ENTRIES"


class BybitDemoControlModeV121(StrEnum):
    HALTED = "HALTED"
    ARMED_NEW_ENTRIES = "ARMED_NEW_ENTRIES"


@dataclass(frozen=True)
class BybitDemoControlEventV121:
    event_id: str
    event_kind: BybitDemoControlEventKindV121
    operator_id: str
    reason: str
    preflight_status: str | None
    preflight_record_sha256: str | None
    preflight_canonical_record: str | None
    preflight_observed_at: datetime | None
    armed_until: datetime | None
    created_at: datetime
    immutable_record: bool = True
    order_submission_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def __post_init__(self) -> None:
        operator_id = _validated_text(self.operator_id, label="operator_id", maximum=128)
        reason = _validated_text(self.reason, label="reason", maximum=1000)
        created_at = _require_aware_utc(self.created_at, "control event created_at")
        object.__setattr__(self, "operator_id", operator_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "created_at", created_at)

        if self.immutable_record is not True:
            raise ValueError("Bybit Demo v121 control event must remain immutable")
        if self.order_submission_supported is not False:
            raise ValueError("Bybit Demo v121 control event cannot support order submission")
        if self.live_mainnet_order_routing_allowed is not False:
            raise ValueError("Bybit Demo v121 control event cannot route live/mainnet orders")
        if not _is_sha256(self.event_id):
            raise ValueError("Bybit Demo v121 control event_id is invalid")

        if self.event_kind is BybitDemoControlEventKindV121.ARM_NEW_ENTRIES:
            self._validate_arm_fields()
        elif self.event_kind is BybitDemoControlEventKindV121.HALT_NEW_ENTRIES:
            self._validate_halt_fields()
        else:  # pragma: no cover - StrEnum constructor blocks this path
            raise ValueError("Bybit Demo v121 control event kind is invalid")

        expected_event_id = compute_control_event_id_v121(self)
        if self.event_id != expected_event_id:
            raise ValueError("Bybit Demo v121 control event_id checksum mismatch")

    def _validate_arm_fields(self) -> None:
        if self.preflight_status != _READY_STATUS:
            raise ValueError("Bybit Demo v121 ARM preflight status is invalid")
        if not _is_sha256(self.preflight_record_sha256):
            raise ValueError("Bybit Demo v121 ARM preflight SHA-256 is invalid")
        if not isinstance(self.preflight_canonical_record, str):
            raise ValueError("Bybit Demo v121 ARM canonical preflight is missing")
        canonical = canonicalize_control_evidence_v121(self.preflight_canonical_record)
        if canonical != self.preflight_canonical_record:
            raise ValueError("Bybit Demo v121 ARM preflight JSON is not canonical")
        if _sha256_text(canonical) != self.preflight_record_sha256:
            raise ValueError("Bybit Demo v121 ARM preflight audit hash mismatch")
        if self.preflight_observed_at is None or self.armed_until is None:
            raise ValueError("Bybit Demo v121 ARM timestamps are incomplete")

        observed_at = _require_aware_utc(
            self.preflight_observed_at,
            "stored preflight observation time",
        )
        armed_until = _require_aware_utc(self.armed_until, "stored control armed_until")
        object.__setattr__(self, "preflight_observed_at", observed_at)
        object.__setattr__(self, "armed_until", armed_until)

        if observed_at > self.created_at:
            raise ValueError("Bybit Demo v121 stored preflight timestamp is in the future")
        if self.created_at - observed_at > _MAX_PREFLIGHT_AGE:
            raise ValueError("Bybit Demo v121 stored preflight is too old")
        ttl = armed_until - self.created_at
        if ttl <= timedelta(0) or ttl > _MAX_ARM_TTL:
            raise ValueError("Bybit Demo v121 ARM TTL must be within (0, 300] seconds")

    def _validate_halt_fields(self) -> None:
        arm_only = (
            self.preflight_status,
            self.preflight_record_sha256,
            self.preflight_canonical_record,
            self.preflight_observed_at,
            self.armed_until,
        )
        if any(value is not None for value in arm_only):
            raise ValueError("Bybit Demo v121 HALT event cannot carry ARM evidence")

    def to_db_values(self) -> tuple[object, ...]:
        return (
            self.event_id,
            self.event_kind.value,
            self.operator_id,
            self.reason,
            self.preflight_status,
            self.preflight_record_sha256,
            self.preflight_canonical_record,
            self.preflight_observed_at,
            self.armed_until,
            self.created_at,
            self.immutable_record,
            self.order_submission_supported,
            self.live_mainnet_order_routing_allowed,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_CONTROL_EVENT_V121",
            "event_id": self.event_id,
            "event_kind": self.event_kind.value,
            "operator_id": self.operator_id,
            "reason": self.reason,
            "preflight_status": self.preflight_status,
            "preflight_record_sha256": self.preflight_record_sha256,
            "preflight_canonical_record": self.preflight_canonical_record,
            "preflight_observed_at": _optional_isoformat(self.preflight_observed_at),
            "armed_until": _optional_isoformat(self.armed_until),
            "created_at": self.created_at.isoformat(),
            "immutable_record": self.immutable_record,
            "order_submission_supported": self.order_submission_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }

    @classmethod
    def from_db_row(cls, row: Mapping[str, object]) -> BybitDemoControlEventV121:
        expected = {
            "event_id",
            "event_kind",
            "operator_id",
            "reason",
            "preflight_status",
            "preflight_record_sha256",
            "preflight_canonical_record",
            "preflight_observed_at",
            "armed_until",
            "created_at",
            "immutable_record",
            "order_submission_supported",
            "live_mainnet_order_routing_allowed",
        }
        if set(row) != expected:
            raise ValueError("Bybit Demo v121 control row keys are invalid")
        try:
            kind = BybitDemoControlEventKindV121(str(row["event_kind"]))
        except ValueError as exc:
            raise ValueError("Bybit Demo v121 control event kind is invalid") from exc
        if not isinstance(row["event_id"], str):
            raise ValueError("Bybit Demo v121 control event_id is invalid")
        if not isinstance(row["operator_id"], str) or not isinstance(row["reason"], str):
            raise ValueError("Bybit Demo v121 control operator metadata is invalid")
        if not isinstance(row["created_at"], datetime):
            raise ValueError("Bybit Demo v121 control created_at is invalid")
        safety_keys = (
            "immutable_record",
            "order_submission_supported",
            "live_mainnet_order_routing_allowed",
        )
        for key in safety_keys:
            if not isinstance(row[key], bool):
                raise ValueError(f"Bybit Demo v121 control {key} marker is invalid")
        return cls(
            event_id=row["event_id"],
            event_kind=kind,
            operator_id=row["operator_id"],
            reason=row["reason"],
            preflight_status=_optional_str(row["preflight_status"], "preflight_status"),
            preflight_record_sha256=_optional_str(
                row["preflight_record_sha256"],
                "preflight_record_sha256",
            ),
            preflight_canonical_record=_optional_str(
                row["preflight_canonical_record"],
                "preflight_canonical_record",
            ),
            preflight_observed_at=_optional_datetime(
                row["preflight_observed_at"],
                "preflight_observed_at",
            ),
            armed_until=_optional_datetime(row["armed_until"], "armed_until"),
            created_at=row["created_at"],
            immutable_record=row["immutable_record"],
            order_submission_supported=row["order_submission_supported"],
            live_mainnet_order_routing_allowed=row["live_mainnet_order_routing_allowed"],
        )


@dataclass(frozen=True)
class BybitDemoControlDecisionV121:
    mode: BybitDemoControlModeV121
    reasons: tuple[str, ...]
    new_entry_allowed: bool
    latest_event_id: str | None
    latest_event_kind: str | None
    armed_until: datetime | None
    immutable_audit: bool = True
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def __post_init__(self) -> None:
        if self.immutable_audit is not True:
            raise ValueError("Bybit Demo v121 decision requires immutable audit")
        if self.order_writes_supported is not False:
            raise ValueError("Bybit Demo v121 decision cannot write orders")
        if self.live_mainnet_order_routing_allowed is not False:
            raise ValueError("Bybit Demo v121 decision cannot route live/mainnet orders")
        if self.mode is BybitDemoControlModeV121.HALTED and self.new_entry_allowed:
            raise ValueError("Bybit Demo v121 HALTED decision cannot allow new entry")
        if self.mode is BybitDemoControlModeV121.ARMED_NEW_ENTRIES and not self.new_entry_allowed:
            raise ValueError("Bybit Demo v121 ARMED decision must report new-entry allowance")


def create_arm_control_event_v121(
    *,
    operator_id: str,
    reason: str,
    preflight_canonical_record: str,
    now: datetime,
    preflight_observed_at: datetime,
    ttl_seconds: int = 120,
) -> BybitDemoControlEventV121:
    created_at = _require_aware_utc(now, "control ARM time")
    observed_at = _require_aware_utc(
        preflight_observed_at,
        "connected preflight observation time",
    )
    operator = _validated_text(operator_id, label="operator_id", maximum=128)
    reason_text = _validated_text(reason, label="reason", maximum=1000)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("Bybit Demo v121 control ARM ttl_seconds must be an integer")
    ttl = timedelta(seconds=ttl_seconds)
    if ttl <= timedelta(0) or ttl > _MAX_ARM_TTL:
        raise ValueError("Bybit Demo v121 control ARM TTL must be within (0, 300] seconds")
    age = created_at - observed_at
    if age < -_MAX_FUTURE_CLOCK_SKEW:
        raise ValueError("Bybit Demo v121 preflight timestamp is in the future")
    if age > _MAX_PREFLIGHT_AGE:
        raise ValueError("Bybit Demo v121 preflight is too old to ARM")
    if observed_at > created_at:
        observed_at = created_at

    canonical = canonicalize_control_evidence_v121(preflight_canonical_record)
    if canonical != preflight_canonical_record:
        raise ValueError("Bybit Demo v121 preflight JSON is not canonical")
    preflight_sha = _sha256_text(canonical)
    armed_until = created_at + ttl
    identity = {
        "event_kind": BybitDemoControlEventKindV121.ARM_NEW_ENTRIES.value,
        "operator_id": operator,
        "reason": reason_text,
        "preflight_status": _READY_STATUS,
        "preflight_record_sha256": preflight_sha,
        "preflight_observed_at": observed_at.isoformat(),
        "armed_until": armed_until.isoformat(),
        "created_at": created_at.isoformat(),
    }
    return BybitDemoControlEventV121(
        event_id=_sha256_json(identity),
        event_kind=BybitDemoControlEventKindV121.ARM_NEW_ENTRIES,
        operator_id=operator,
        reason=reason_text,
        preflight_status=_READY_STATUS,
        preflight_record_sha256=preflight_sha,
        preflight_canonical_record=canonical,
        preflight_observed_at=observed_at,
        armed_until=armed_until,
        created_at=created_at,
    )


def create_halt_control_event_v121(
    *,
    operator_id: str,
    reason: str,
    now: datetime,
) -> BybitDemoControlEventV121:
    created_at = _require_aware_utc(now, "control HALT time")
    operator = _validated_text(operator_id, label="operator_id", maximum=128)
    reason_text = _validated_text(reason, label="reason", maximum=1000)
    identity = {
        "event_kind": BybitDemoControlEventKindV121.HALT_NEW_ENTRIES.value,
        "operator_id": operator,
        "reason": reason_text,
        "created_at": created_at.isoformat(),
    }
    return BybitDemoControlEventV121(
        event_id=_sha256_json(identity),
        event_kind=BybitDemoControlEventKindV121.HALT_NEW_ENTRIES,
        operator_id=operator,
        reason=reason_text,
        preflight_status=None,
        preflight_record_sha256=None,
        preflight_canonical_record=None,
        preflight_observed_at=None,
        armed_until=None,
        created_at=created_at,
    )


def compute_control_event_id_v121(event: BybitDemoControlEventV121) -> str:
    if event.event_kind is BybitDemoControlEventKindV121.ARM_NEW_ENTRIES:
        if event.preflight_record_sha256 is None:
            raise ValueError("Bybit Demo v121 ARM preflight SHA-256 is missing")
        if event.preflight_observed_at is None or event.armed_until is None:
            raise ValueError("Bybit Demo v121 ARM timestamps are incomplete")
        identity = {
            "event_kind": event.event_kind.value,
            "operator_id": event.operator_id,
            "reason": event.reason,
            "preflight_status": event.preflight_status,
            "preflight_record_sha256": event.preflight_record_sha256,
            "preflight_observed_at": event.preflight_observed_at.isoformat(),
            "armed_until": event.armed_until.isoformat(),
            "created_at": event.created_at.isoformat(),
        }
    else:
        identity = {
            "event_kind": event.event_kind.value,
            "operator_id": event.operator_id,
            "reason": event.reason,
            "created_at": event.created_at.isoformat(),
        }
    return _sha256_json(identity)


def decision_from_control_event_v121(
    event: BybitDemoControlEventV121 | None,
    *,
    now: datetime,
) -> BybitDemoControlDecisionV121:
    observed_at = _require_aware_utc(now, "control decision time")
    if event is None:
        return _halted_decision("DEMO_CONTROL_NO_EVENT_DEFAULT_HALT")
    if event.event_kind is BybitDemoControlEventKindV121.HALT_NEW_ENTRIES:
        return _halted_decision(
            "DEMO_CONTROL_OPERATOR_HALT",
            event=event,
        )
    if event.created_at - observed_at > _MAX_FUTURE_CLOCK_SKEW:
        return _halted_decision("DEMO_CONTROL_EVENT_CLOCK_INVALID", event=event)
    if event.armed_until is None:
        return _halted_decision("DEMO_CONTROL_EVENT_INVALID", event=event)
    if observed_at >= event.armed_until:
        return _halted_decision("DEMO_CONTROL_ARM_EXPIRED", event=event)
    return BybitDemoControlDecisionV121(
        mode=BybitDemoControlModeV121.ARMED_NEW_ENTRIES,
        reasons=(),
        new_entry_allowed=True,
        latest_event_id=event.event_id,
        latest_event_kind=event.event_kind.value,
        armed_until=event.armed_until,
    )


def canonicalize_control_evidence_v121(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Bybit Demo v121 canonical preflight JSON is required")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Bybit Demo v121 canonical preflight JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Bybit Demo v121 canonical preflight must be a JSON object")
    return _canonical_json(decoded)


def _halted_decision(
    reason: str,
    *,
    event: BybitDemoControlEventV121 | None = None,
) -> BybitDemoControlDecisionV121:
    return BybitDemoControlDecisionV121(
        mode=BybitDemoControlModeV121.HALTED,
        reasons=(reason,),
        new_entry_allowed=False,
        latest_event_id=None if event is None else event.event_id,
        latest_event_kind=None if event is None else event.event_kind.value,
        armed_until=None if event is None else event.armed_until,
    )


def _validated_text(value: str, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Bybit Demo v121 control {label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"Bybit Demo v121 control {label} is invalid")
    return normalized


def _require_aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Bybit Demo v121 {label} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"Bybit Demo v121 control {label} is invalid")
    return value


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Bybit Demo v121 control {label} is invalid")
    return value


def _optional_isoformat(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


__all__ = [
    "BybitDemoControlDecisionV121",
    "BybitDemoControlEventKindV121",
    "BybitDemoControlEventV121",
    "BybitDemoControlModeV121",
    "canonicalize_control_evidence_v121",
    "compute_control_event_id_v121",
    "create_arm_control_event_v121",
    "create_halt_control_event_v121",
    "decision_from_control_event_v121",
]
