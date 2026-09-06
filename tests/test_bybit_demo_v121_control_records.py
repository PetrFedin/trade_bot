from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.execution.bybit_demo_v121_control_records import (
    BybitDemoControlEventKindV121,
    BybitDemoControlModeV121,
    canonicalize_control_evidence_v121,
    create_arm_control_event_v121,
    create_halt_control_event_v121,
    decision_from_control_event_v121,
)

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
_PREFLIGHT_PAYLOAD = {
    "schema": "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1",
    "status": "READY_FOR_MANUAL_OPERATOR_APPROVAL",
    "passed": True,
    "reasons": [],
    "account": {
        "margin_mode": "REGULAR_MARGIN",
        "unified_margin_status": 1,
        "positive_equity": True,
        "positive_available_balance": True,
        "usdt_wallet_visible": True,
        "open_position_count": 0,
        "open_position_symbols": [],
        "open_order_count": 0,
        "open_order_symbols": [],
    },
    "credential": {
        "read_only_api_key_verified": True,
        "ip_binding_present": True,
    },
    "durable_state": {
        "active_checkpoint_present": False,
        "active_checkpoint_symbol": None,
        "runtime_lease_present": False,
        "required_relations_present": True,
        "append_only_triggers_present": True,
        "approval_record_count": 0,
        "provenance_record_count": 0,
        "terminal_record_count": 0,
    },
    "demo_host_verified": True,
    "credentials_verified_by_authenticated_reads": True,
    "preflight_only": True,
    "trade_actionable": False,
    "order_writes_supported": False,
    "live_mainnet_order_routing_allowed": False,
}
_CANONICAL_PREFLIGHT = json.dumps(
    _PREFLIGHT_PAYLOAD,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
)


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_arm_event_preserves_historical_canonical_event_id_formula() -> None:
    event = create_arm_control_event_v121(
        operator_id="operator-1",
        reason="manual demo approval",
        preflight_canonical_record=_CANONICAL_PREFLIGHT,
        now=_NOW,
        preflight_observed_at=_NOW - timedelta(seconds=10),
        ttl_seconds=120,
    )

    assert event.event_kind is BybitDemoControlEventKindV121.ARM_NEW_ENTRIES
    assert event.preflight_record_sha256 == (
        "eee67955e50e92a5b1106a233fe82a0b32a3f58fb43e62391fd685130f0239f5"
    )
    assert event.event_id == (
        "fcaa651bc694631fc7911d444018538e6b8454ae9c3270296ca6993f8e212495"
    )
    assert event.armed_until == _NOW + timedelta(seconds=120)
    assert event.order_submission_supported is False
    assert event.live_mainnet_order_routing_allowed is False


def test_halt_event_preserves_historical_canonical_event_id_formula() -> None:
    event = create_halt_control_event_v121(
        operator_id="operator-1",
        reason="manual halt",
        now=_NOW,
    )

    assert event.event_kind is BybitDemoControlEventKindV121.HALT_NEW_ENTRIES
    assert event.event_id == (
        "4fa53a77f73f4e714661769c81110bfbe0ec2df0c2cf617dd837606ede0250c4"
    )
    assert event.preflight_record_sha256 is None
    assert event.armed_until is None


def test_arm_rejects_noncanonical_or_tampered_evidence() -> None:
    noncanonical = json.dumps(_PREFLIGHT_PAYLOAD, sort_keys=False)
    with pytest.raises(ValueError, match="not canonical"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=noncanonical,
            now=_NOW,
            preflight_observed_at=_NOW,
        )

    event = create_arm_control_event_v121(
        operator_id="operator-1",
        reason="manual demo approval",
        preflight_canonical_record=_CANONICAL_PREFLIGHT,
        now=_NOW,
        preflight_observed_at=_NOW,
    )
    with pytest.raises(ValueError, match="audit hash mismatch"):
        replace(event, preflight_record_sha256="a" * 64)


def test_arm_neutral_evidence_preserves_historical_safety_invariants() -> None:
    with pytest.raises(ValueError, match="keys are invalid"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record="{}",
            now=_NOW,
            preflight_observed_at=_NOW,
        )

    no_equity = copy.deepcopy(_PREFLIGHT_PAYLOAD)
    no_equity["account"]["positive_equity"] = False
    with pytest.raises(ValueError, match="positive Demo capital"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=_canonical(no_equity),
            now=_NOW,
            preflight_observed_at=_NOW,
        )

    writable_key = copy.deepcopy(_PREFLIGHT_PAYLOAD)
    writable_key["credential"]["read_only_api_key_verified"] = False
    with pytest.raises(ValueError, match="read-only key"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=_canonical(writable_key),
            now=_NOW,
            preflight_observed_at=_NOW,
        )

    busy_runtime = copy.deepcopy(_PREFLIGHT_PAYLOAD)
    busy_runtime["durable_state"]["runtime_lease_present"] = True
    with pytest.raises(ValueError, match="idle durable runtime"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=_canonical(busy_runtime),
            now=_NOW,
            preflight_observed_at=_NOW,
        )

    order_capable = copy.deepcopy(_PREFLIGHT_PAYLOAD)
    order_capable["order_writes_supported"] = True
    with pytest.raises(ValueError, match="order-writing preflight"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=_canonical(order_capable),
            now=_NOW,
            preflight_observed_at=_NOW,
        )


def test_arm_ttl_age_future_skew_and_text_bounds_fail_closed() -> None:
    for ttl in (0, -1, 301):
        with pytest.raises(ValueError, match="TTL"):
            create_arm_control_event_v121(
                operator_id="operator-1",
                reason="manual demo approval",
                preflight_canonical_record=_CANONICAL_PREFLIGHT,
                now=_NOW,
                preflight_observed_at=_NOW,
                ttl_seconds=ttl,
            )

    with pytest.raises(ValueError, match="too old"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=_CANONICAL_PREFLIGHT,
            now=_NOW,
            preflight_observed_at=_NOW - timedelta(seconds=31),
        )
    with pytest.raises(ValueError, match="future"):
        create_arm_control_event_v121(
            operator_id="operator-1",
            reason="manual demo approval",
            preflight_canonical_record=_CANONICAL_PREFLIGHT,
            now=_NOW,
            preflight_observed_at=_NOW + timedelta(seconds=6),
        )
    clamped = create_arm_control_event_v121(
        operator_id="operator-1",
        reason="manual demo approval",
        preflight_canonical_record=_CANONICAL_PREFLIGHT,
        now=_NOW,
        preflight_observed_at=_NOW + timedelta(seconds=5),
    )
    assert clamped.preflight_observed_at == _NOW

    with pytest.raises(ValueError, match="operator_id"):
        create_halt_control_event_v121(operator_id=" ", reason="halt", now=_NOW)
    with pytest.raises(ValueError, match="reason"):
        create_halt_control_event_v121(
            operator_id="operator-1",
            reason="x" * 1001,
            now=_NOW,
        )


def test_decision_defaults_and_expires_fail_closed() -> None:
    default = decision_from_control_event_v121(None, now=_NOW)
    assert default.mode is BybitDemoControlModeV121.HALTED
    assert default.new_entry_allowed is False
    assert default.reasons == ("DEMO_CONTROL_NO_EVENT_DEFAULT_HALT",)

    arm = create_arm_control_event_v121(
        operator_id="operator-1",
        reason="manual demo approval",
        preflight_canonical_record=_CANONICAL_PREFLIGHT,
        now=_NOW,
        preflight_observed_at=_NOW,
        ttl_seconds=120,
    )
    armed = decision_from_control_event_v121(arm, now=_NOW + timedelta(seconds=1))
    assert armed.mode is BybitDemoControlModeV121.ARMED_NEW_ENTRIES
    assert armed.new_entry_allowed is True

    expired = decision_from_control_event_v121(arm, now=_NOW + timedelta(seconds=120))
    assert expired.mode is BybitDemoControlModeV121.HALTED
    assert expired.new_entry_allowed is False
    assert expired.reasons == ("DEMO_CONTROL_ARM_EXPIRED",)

    halt = create_halt_control_event_v121(
        operator_id="operator-1",
        reason="manual halt",
        now=_NOW,
    )
    halted = decision_from_control_event_v121(halt, now=_NOW)
    assert halted.mode is BybitDemoControlModeV121.HALTED
    assert halted.reasons == ("DEMO_CONTROL_OPERATOR_HALT",)


def test_canonical_evidence_requires_json_object() -> None:
    assert canonicalize_control_evidence_v121(_CANONICAL_PREFLIGHT) == _CANONICAL_PREFLIGHT
    with pytest.raises(ValueError, match="JSON object"):
        canonicalize_control_evidence_v121("[]")
    with pytest.raises(ValueError, match="invalid"):
        canonicalize_control_evidence_v121("{")
