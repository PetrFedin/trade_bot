from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_runtime_lease_recovery import (
    BybitDemoRuntimeLeaseRecoveryStatus,
    PostgresBybitDemoRuntimeLeaseRecovery,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or explicitly recover an orphaned Bybit Demo canonical runtime lease. "
            "No age-based or automatic takeover is supported."
        )
    )
    parser.add_argument("--mode", choices=("inspect", "recover"), default="inspect")
    parser.add_argument("--expected-owner-sha256", default="")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--process-stop-evidence", default="")
    parser.add_argument("--confirmation", default="")
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-runtime-lease-recovery.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = os.environ.get("BYBIT_DEMO_DATABASE_DSN", "").strip()
    if not dsn:
        payload = _failure("DEMO_DATABASE_CONFIG_UNAVAILABLE", mode=args.mode)
        _emit(args.output, payload)
        return 2

    recovery = PostgresBybitDemoRuntimeLeaseRecovery(dsn)
    try:
        if args.mode == "inspect":
            inspection = recovery.inspect()
            payload = inspection.to_payload()
            payload["mode"] = "inspect"
            exit_code = (
                2
                if inspection.status is BybitDemoRuntimeLeaseRecoveryStatus.BLOCKED
                else 0
            )
        else:
            receipt = recovery.recover(
                expected_lease_owner_sha256=args.expected_owner_sha256,
                operator_id=args.operator_id,
                reason=args.reason,
                process_stop_evidence=args.process_stop_evidence,
                confirmation_phrase=args.confirmation,
            )
            payload = receipt.to_payload()
            payload["mode"] = "recover"
            exit_code = 0
    except Exception as exc:  # noqa: BLE001 - production artifact must stay sanitized.
        payload = _failure(type(exc).__name__, mode=args.mode)
        exit_code = 2
    _emit(args.output, payload)
    return exit_code


def _failure(error_type: str, *, mode: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_FAILURE_V1",
        "status": "RECOVERY_FAILED",
        "mode": mode,
        "error_type": error_type,
        "automatic_recovery_allowed": False,
        "automatic_stale_takeover_allowed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "database_identity_exposed": False,
    }


def _emit(path: str, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    print(text, flush=True)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, target)


if __name__ == "__main__":
    raise SystemExit(main())
