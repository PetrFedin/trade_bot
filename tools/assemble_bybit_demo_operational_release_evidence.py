from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseStage,
    assemble_bybit_demo_operational_release_evidence,
    load_json_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble exact-head Bybit Demo operational evidence without database, broker, "
            "control-plane or order mutations."
        )
    )
    parser.add_argument("--git-sha", default=os.environ.get("GITHUB_SHA", ""), required=False)
    parser.add_argument("--activation-readiness", type=Path, required=True)
    parser.add_argument("--session-start", type=Path)
    parser.add_argument("--supervisor", type=Path)
    parser.add_argument("--operational-entry", type=Path)
    parser.add_argument("--recovery-receipt", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/bybit-demo-operational-release-evidence.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        activation, activation_sha = load_json_evidence(args.activation_readiness)
        payloads: dict[str, dict[str, Any] | None] = {
            "activation_readiness": activation,
            "session_start": None,
            "supervisor": None,
            "operational_entry": None,
            "recovery_receipt": None,
        }
        evidence_sha256: dict[str, str] = {
            "activation_readiness": activation_sha,
        }
        for name, path in (
            ("session_start", args.session_start),
            ("supervisor", args.supervisor),
            ("operational_entry", args.operational_entry),
            ("recovery_receipt", args.recovery_receipt),
        ):
            if path is None:
                continue
            item, digest = load_json_evidence(path)
            payloads[name] = item
            evidence_sha256[name] = digest

        result = assemble_bybit_demo_operational_release_evidence(
            git_sha=args.git_sha,
            activation_readiness=activation,
            session_start=payloads["session_start"],
            supervisor=payloads["supervisor"],
            operational_entry=payloads["operational_entry"],
            recovery_receipt=payloads["recovery_receipt"],
            evidence_sha256=evidence_sha256,
        )
        output = result.to_payload()
        exit_code = 0 if result.stage is not BybitDemoOperationalReleaseStage.BLOCKED else 2
    except Exception as exc:  # noqa: BLE001 - artifact exposes only the error class.
        output = _failure_payload(type(exc).__name__, git_sha=args.git_sha)
        exit_code = 2

    _emit(args.output, output)
    return exit_code


def _failure_payload(error_type: str, *, git_sha: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_OPERATIONAL_RELEASE_EVIDENCE_V1",
        "stage": "BLOCKED",
        "passed": False,
        "reasons": [f"ASSEMBLY_FAILED:{error_type}"],
        "git_sha": git_sha if _looks_like_git_sha(git_sha) else None,
        "evidence_sha256": {},
        "next_required_evidence": None,
        "release_gate_complete": False,
        "operator_action_required": True,
        "automatic_activation_allowed": False,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _looks_like_git_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _emit(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)
    print(text, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
