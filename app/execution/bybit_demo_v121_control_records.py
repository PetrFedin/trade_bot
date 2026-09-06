from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

_READY_STATUS = "READY_FOR_MANUAL_OPERATOR_APPROVAL"
_PREFLIGHT_SCHEMA = "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1"
_MAX_ARM_TTL = timedelta(minutes=5)
_MAX_PREFLIGHT_AGE = timedelta(seconds=30)
_MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)
_PREFLIGHT_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "status",
        "passed",
        "reasons",
        "account",
        "credential",
        "durable_state",
        "demo_host_verified",
        "credentials_verified_by_authenticated_reads",
        "preflight_only",
        "trade_actionable",
        "order_writes_supported",
        "live_mainnet_order_routing_allowed",
    }
)
_PREFLIGHT_ACCOUNT_KEYS = frozenset(
    {
        "margin_mode",
        "unified_margin_status",
        "positive_equity",
        "positive_available_balance",
        "usdt_wallet_visible",
        "open_position_count",
        "open_position_symbols",
        "open_order_count",
        "open_order_symbols",
    }
)
_PREFLIGHT_CREDENTIAL_KEYS = frozenset(
    {"read_only_api_key_verified", "ip_binding_present"}
)
_PREFLIGHT_DURABLE_KEYS = frozenset(
    {
        "active_checkpoint_present",
        "active_checkpoint_symbol",
        "runtime_lease_present",
        "required_relations_present",
        "append_only_triggers_present",
        "approval_record_count",
        "provenance_record_count",
        "terminal_record_count",
    }
)


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
        canonical = validate_arm_preflight_evidence_v121(self.preflight_canonical_record)
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

    canonical = validate_arm_preflight_evidence_v121(preflight_canonical_record)
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


def validate_arm_preflight_evidence_v121(value: str) -> str:
    canonical = canonicalize_control_evidence_v121(value)
    if canonical != value:
        raise ValueError("Bybit Demo v121 ARM preflight JSON is not canonical")
    decoded = json.loads(canonical)
    _require_exact_object_keys(
        decoded,
        _PREFLIGHT_TOP_LEVEL_KEYS,
        "preflight evidence",
    )
    if decoded["schema"] != _PREFLIGHT_SCHEMA:
        raise ValueError("Bybit Demo v121 ARM preflight schema is invalid")
    if decoded["status"] != _READY_STATUS or decoded["passed"] is not True:
        raise ValueError("Bybit Demo v121 ARM requires clean connected preflight")
    reasons = decoded["reasons"]
    if not isinstance(reasons, list) or reasons or any(not isinstance(item, str) for item in reasons):
        raise ValueError("Bybit Demo v121 ARM rejected preflight reasons")

    account = _require_exact_object(
        decoded["account"],
        _PREFLIGHT_ACCOUNT_KEYS,
        "preflight account",
    )
    margin_mode = account["margin_mode"]
    if not isinstance(margin_mode, str) or not margin_mode.strip():
        raise ValueError("Bybit Demo v121 ARM preflight margin mode is invalid")
    _require_nonnegative_int(account["unified_margin_status"], "unified margin status")
    positive_equity = _require_bool(account["positive_equity"], "positive equity")
    positive_balance = _require_bool(
        account["positive_available_balance"],
        "positive available balance",
    )
    _require_bool(account["usdt_wallet_visible"], "USDT wallet visibility")
    open_positions = _require_nonnegative_int(
        account["open_position_count"],
        "open position count",
    )
    position_symbols = _require_string_list(
        account["open_position_symbols"],
        "open position symbols",
    )
    open_orders = _require_nonnegative_int(account["open_order_count"], "open order count")
    order_symbols = _require_string_list(
        account["open_order_symbols"],
        "open order symbols",
    )
    if not positive_equity or not positive_balance:
        raise ValueError("Bybit Demo v121 ARM requires positive Demo capital")
    if open_positions != 0 or position_symbols:
        raise ValueError("Bybit Demo v121 ARM requires flat exchange position state")
    if open_orders != 0 or order_symbols:
        raise ValueError("Bybit Demo v121 ARM requires no open exchange orders")

    credential = _require_exact_object(
        decoded["credential"],
        _PREFLIGHT_CREDENTIAL_KEYS,
        "preflight credential",
    )
    if not _require_bool(
        credential["read_only_api_key_verified"],
        "read-only API key verification",
    ):
        raise ValueError("Bybit Demo v121 ARM requires verified read-only key")
    _require_bool(credential["ip_binding_present"], "API key IP binding")

    durable = _require_exact_object(
        decoded["durable_state"],
        _PREFLIGHT_DURABLE_KEYS,
        "preflight durable state",
    )
    active_checkpoint = _require_bool(
        durable["active_checkpoint_present"],
        "active checkpoint presence",
    )
    active_symbol = durable["active_checkpoint_symbol"]
    if active_symbol is not None and not isinstance(active_symbol, str):
        raise ValueError("Bybit Demo v121 ARM active checkpoint symbol is invalid")
    runtime_lease = _require_bool(
        durable["runtime_lease_present"],
        "runtime lease presence",
    )
    relations_ready = _require_bool(
        durable["required_relations_present"],
        "required relation readiness",
    )
    triggers_ready = _require_bool(
        durable["append_only_triggers_present"],
        "append-only trigger readiness",
    )
    for key in ("approval_record_count", "provenance_record_count", "terminal_record_count"):
        _require_nonnegative_int(durable[key], key.replace("_", " "))
    if active_checkpoint or active_symbol is not None or runtime_lease:
        raise ValueError("Bybit Demo v121 ARM requires idle durable runtime")
    if not relations_ready or not triggers_ready:
        raise ValueError("Bybit Demo v121 ARM requires verified v119/v120 schema")

    if not _require_bool(decoded["demo_host_verified"], "Demo host verification"):
        raise ValueError("Bybit Demo v121 ARM requires verified Demo host")
    if not _require_bool(
        decoded["credentials_verified_by_authenticated_reads"],
        "authenticated credential verification",
    ):
        raise ValueError("Bybit Demo v121 ARM requires authenticated read verification")
    if not _require_bool(decoded["preflight_only"], "preflight-only marker"):
        raise ValueError("Bybit Demo v121 ARM requires preflight-only evidence")
    if _require_bool(decoded["trade_actionable"], "trade-actionable marker"):
        raise ValueError("Bybit Demo v121 ARM rejected actionable preflight")
    if _require_bool(decoded["order_writes_supported"], "order-write marker"):
        raise ValueError("Bybit Demo v121 ARM rejected order-writing preflight")
    if _require_bool(
        decoded["live_mainnet_order_routing_allowed"],
        "live/mainnet routing marker",
    ):
        raise ValueError("Bybit Demo v121 ARM rejected mainnet-capable preflight")
    return canonical


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


def _require_exact_object(value: object, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Bybit Demo v121 ARM {label} is invalid")
    _require_exact_object_keys(value, keys, label)
    return value


def _require_exact_object_keys(value: Mapping[str, object], keys: frozenset[str], label: str) -> None:
    if set(value) != keys:
        raise ValueError(f"Bybit Demo v121 ARM {label} keys are invalid")


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Bybit Demo v121 ARM {label} is invalid")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Bybit Demo v121 ARM {label} is invalid")
    return value


def _require_string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Bybit Demo v121 ARM {label} is invalid")
    return tuple(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
    "validate_arm_preflight_evidence_v121",
]
