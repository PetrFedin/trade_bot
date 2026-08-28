from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_fixed_egress import (
    BybitDemoFixedEgressPreflightAccountClient,
)
from app.execution.bybit_demo_session_start import (
    BybitDemoSessionStartStatus,
    PostgresBybitDemoSessionStartCoordinator,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read or explicitly initialize the restart-safe Bybit Demo risk session. "
            "There is intentionally no reset mode."
        )
    )
    parser.add_argument("--mode", choices=("status", "initialize"), default="status")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--git-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-session-risk.json",
    )
    return parser


def _write(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _failure(error_type: str, *, mode: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_SESSION_START_V1",
        "mode": mode,
        "status": "SESSION_OPERATION_FAILED",
        "passed": False,
        "error_type": error_type,
        "session_initialized": False,
        "worker_session_ready": False,
        "fixed_egress_required": True,
        "explicit_operator_action_required": True,
        "automatic_reset_allowed": False,
        "trading_credential_required": False,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "database_identity_exposed": False,
        "bybit_credentials_exposed": False,
    }


def main() -> int:
    args = _parser().parse_args()
    git_sha = _artifact_git_sha(args.git_sha)
    dsn = os.environ.get("BYBIT_DEMO_DATABASE_DSN", "")
    if not dsn:
        payload = _failure("DEMO_DATABASE_CONFIG_UNAVAILABLE", mode=args.mode) | {
            "git_sha": git_sha,
        }
        _write(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2

    try:
        coordinator = PostgresBybitDemoSessionStartCoordinator(dsn)
        if args.mode == "status":
            result = coordinator.read_status()
        else:
            api_key = os.environ.get("BYBIT_DEMO_READONLY_API_KEY", "")
            api_secret = os.environ.get("BYBIT_DEMO_READONLY_API_SECRET", "")
            if not api_key or not api_secret:
                raise RuntimeError("Demo read-only credential configuration is unavailable")
            result = coordinator.initialize(
                BybitDemoFixedEgressPreflightAccountClient(
                    api_key=api_key,
                    api_secret=api_secret,
                ),
                confirmation_phrase=args.confirmation,
                operator_id=args.operator_id,
                reason=args.reason,
                git_sha=args.git_sha,
                now=datetime.now(UTC),
            )
        payload = result.to_payload() | {"mode": args.mode, "git_sha": git_sha}
        if args.mode == "status":
            exit_code = 0 if result.passed else 2
        else:
            exit_code = (
                0
                if result.status is BybitDemoSessionStartStatus.INITIALIZED_NOW
                else 2
            )
    except Exception as exc:  # noqa: BLE001 - never serialize DSN/credential-bearing errors.
        payload = _failure(type(exc).__name__, mode=args.mode) | {"git_sha": git_sha}
        exit_code = 2

    _write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return exit_code


def _artifact_git_sha(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None


if __name__ == "__main__":
    raise SystemExit(main())
