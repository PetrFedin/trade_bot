from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightStatus,
    PostgresBybitDemoOperationalStateReader,
)
from app.execution.bybit_demo_control_plane import PostgresBybitDemoControlPlane
from app.execution.bybit_demo_fixed_egress import (
    BybitDemoFixedEgressPreflightAccountClient,
    require_fixed_egress_ready_for_arm,
    run_bybit_demo_fixed_egress_connected_preflight,
)

_ARM_CONFIRMATION = "ARM_BYBIT_DEMO_NEW_ENTRIES"
_HALT_CONFIRMATION = "HALT_BYBIT_DEMO_NEW_ENTRIES"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read or explicitly change the fail-closed Bybit Demo new-entry control state."
    )
    parser.add_argument("--mode", choices=("status", "arm", "halt"), default="status")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--ttl-seconds", type=int, default=120)
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-control-plane.json",
    )
    return parser


def _write(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _base(*, mode: str, status: str, passed: bool) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_CONTROL_OPERATION_V1",
        "mode": mode,
        "status": status,
        "passed": passed,
        "fixed_egress_required": True,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _failure(error_type: str, *, mode: str) -> dict[str, Any]:
    return _base(mode=mode, status="CONTROL_OPERATION_FAILED", passed=False) | {
        "error_type": error_type,
        "database_identity_exposed": False,
        "bybit_credentials_exposed": False,
    }


def main() -> int:
    args = _parser().parse_args()
    dsn = os.environ.get("BYBIT_DEMO_DATABASE_DSN", "")
    if not dsn:
        payload = _failure("DEMO_DATABASE_CONFIG_UNAVAILABLE", mode=args.mode)
        _write(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2

    try:
        plane = PostgresBybitDemoControlPlane(dsn)
        if args.mode == "status":
            decision = plane.read_decision(now=datetime.now(UTC))
            payload = _base(mode=args.mode, status="STATUS_READ", passed=True) | {
                "decision": decision.to_payload()
            }
            exit_code = 0
        elif args.mode == "halt":
            if args.confirmation != _HALT_CONFIRMATION:
                raise ValueError("invalid HALT confirmation")
            receipt = plane.halt_new_entries(
                operator_id=args.operator_id,
                reason=args.reason,
                now=datetime.now(UTC),
            )
            decision = plane.read_decision(now=datetime.now(UTC))
            payload = _base(mode=args.mode, status="HALTED", passed=True) | {
                "receipt": receipt.to_payload(),
                "decision": decision.to_payload(),
            }
            exit_code = 0
        else:
            if args.confirmation != _ARM_CONFIRMATION:
                raise ValueError("invalid ARM confirmation")
            api_key = os.environ.get("BYBIT_DEMO_READONLY_API_KEY", "")
            api_secret = os.environ.get("BYBIT_DEMO_READONLY_API_SECRET", "")
            if not api_key or not api_secret:
                raise RuntimeError("Demo read-only credential configuration is unavailable")
            preflight = run_bybit_demo_fixed_egress_connected_preflight(
                BybitDemoFixedEgressPreflightAccountClient(
                    api_key=api_key,
                    api_secret=api_secret,
                ),
                PostgresBybitDemoOperationalStateReader(dsn),
            )
            observed_at = datetime.now(UTC)
            if (
                preflight.status
                is not BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
            ):
                decision = plane.read_decision(now=observed_at)
                payload = _base(
                    mode=args.mode,
                    status="ARM_BLOCKED_BY_CONNECTED_PREFLIGHT",
                    passed=False,
                ) | {
                    "preflight": preflight.to_payload(),
                    "decision": decision.to_payload(),
                }
                exit_code = 2
            else:
                require_fixed_egress_ready_for_arm(preflight)
                receipt = plane.arm_new_entries(
                    preflight,
                    operator_id=args.operator_id,
                    reason=args.reason,
                    now=observed_at,
                    preflight_observed_at=observed_at,
                    ttl_seconds=args.ttl_seconds,
                )
                decision = plane.read_decision(now=datetime.now(UTC))
                payload = _base(mode=args.mode, status="ARMED", passed=True) | {
                    "preflight": preflight.to_payload(),
                    "receipt": receipt.to_payload(),
                    "decision": decision.to_payload(),
                }
                exit_code = 0
    except Exception as exc:  # noqa: BLE001 - never serialize secrets/DSN in failures.
        payload = _failure(type(exc).__name__, mode=args.mode)
        exit_code = 2

    _write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
