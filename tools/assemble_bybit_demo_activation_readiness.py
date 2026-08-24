from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_activation_readiness import (
    assemble_bybit_demo_activation_readiness,
    load_json_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble sanitized Bybit Demo infrastructure evidence into a fail-closed "
            "activation-readiness manifest."
        )
    )
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--postgres", required=True)
    parser.add_argument("--connected-preflight", required=True)
    parser.add_argument("--trading-credential", required=True)
    parser.add_argument("--control-status", required=True)
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-activation-readiness.json",
    )
    return parser


def _write(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _failure(error_type: str, *, git_sha: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_ACTIVATION_READINESS_V1",
        "status": "ASSEMBLY_FAILED",
        "passed": False,
        "reasons": ["READINESS_EVIDENCE_INVALID"],
        "git_sha": git_sha if len(git_sha) == 40 else "INVALID",
        "ready_for_explicit_arm": False,
        "ready_for_exact_trade_approval": False,
        "operator_action_required": True,
        "arm_performed": False,
        "trade_actionable": False,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "error_type": error_type,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        postgres, postgres_sha = load_json_evidence(args.postgres)
        connected, connected_sha = load_json_evidence(args.connected_preflight)
        credential, credential_sha = load_json_evidence(args.trading_credential)
        control, control_sha = load_json_evidence(args.control_status)
        result = assemble_bybit_demo_activation_readiness(
            git_sha=args.git_sha,
            postgres_payload=postgres,
            connected_preflight_payload=connected,
            trading_credential_payload=credential,
            control_status_payload=control,
            evidence_sha256={
                "postgres": postgres_sha,
                "connected": connected_sha,
                "credential": credential_sha,
                "control": control_sha,
            },
        )
        payload = result.to_payload()
        exit_code = 0 if result.passed else 2
    except Exception as exc:  # noqa: BLE001 - never serialize evidence contents or secrets.
        payload = _failure(type(exc).__name__, git_sha=args.git_sha)
        exit_code = 2

    _write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
