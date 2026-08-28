from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseStage,
    load_json_evidence,
)
from app.execution.bybit_demo_operational_release_logical_db_binding import (
    assemble_logical_db_bound_bybit_demo_operational_release_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble exact-head Bybit Demo operational evidence without database, broker, "
            "control-plane or order mutations."
        )
    )
    parser.add_argument("--git-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--run-metadata", type=Path, required=True)
    parser.add_argument("--activation-readiness", type=Path, required=True)
    parser.add_argument("--activation-readiness-zone", type=Path, required=True)
    parser.add_argument("--session-start", type=Path)
    parser.add_argument("--session-start-zone", type=Path)
    parser.add_argument("--supervisor", type=Path)
    parser.add_argument("--supervisor-zone", type=Path)
    parser.add_argument("--arm-control", type=Path)
    parser.add_argument("--arm-control-zone", type=Path)
    parser.add_argument("--operational-entry", type=Path)
    parser.add_argument("--operational-entry-zone", type=Path)
    parser.add_argument("--halt-control", type=Path)
    parser.add_argument("--halt-control-zone", type=Path)
    parser.add_argument("--recovery-receipt", type=Path)
    parser.add_argument("--recovery-receipt-zone", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/bybit-demo-operational-release-evidence.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_metadata, run_metadata_sha = load_json_evidence(args.run_metadata)
        activation, activation_sha = load_json_evidence(args.activation_readiness)
        activation_zone, activation_zone_sha = load_json_evidence(
            args.activation_readiness_zone
        )
        payloads: dict[str, dict[str, Any] | None] = {
            "activation_readiness": activation,
            "session_start": None,
            "supervisor": None,
            "arm_control": None,
            "operational_entry": None,
            "halt_control": None,
            "recovery_receipt": None,
        }
        evidence_sha256: dict[str, str] = {
            "activation_readiness": activation_sha,
        }
        zone_bindings: dict[str, dict[str, Any]] = {
            "activation_readiness": activation_zone,
        }
        zone_binding_sha256: dict[str, str] = {
            "activation_readiness": activation_zone_sha,
        }
        for name, evidence_path, zone_path in (
            ("session_start", args.session_start, args.session_start_zone),
            ("supervisor", args.supervisor, args.supervisor_zone),
            ("arm_control", args.arm_control, args.arm_control_zone),
            (
                "operational_entry",
                args.operational_entry,
                args.operational_entry_zone,
            ),
            ("halt_control", args.halt_control, args.halt_control_zone),
            (
                "recovery_receipt",
                args.recovery_receipt,
                args.recovery_receipt_zone,
            ),
        ):
            if evidence_path is not None:
                item, digest = load_json_evidence(evidence_path)
                payloads[name] = item
                evidence_sha256[name] = digest
            if zone_path is not None:
                zone_item, zone_digest = load_json_evidence(zone_path)
                zone_bindings[name] = zone_item
                zone_binding_sha256[name] = zone_digest

        result = assemble_logical_db_bound_bybit_demo_operational_release_evidence(
            git_sha=args.git_sha,
            activation_readiness=activation,
            session_start=payloads["session_start"],
            supervisor=payloads["supervisor"],
            arm_control=payloads["arm_control"],
            operational_entry=payloads["operational_entry"],
            halt_control=payloads["halt_control"],
            recovery_receipt=payloads["recovery_receipt"],
            evidence_sha256=evidence_sha256,
            source_run_metadata=run_metadata,
            source_run_metadata_sha256=run_metadata_sha,
            zone_bindings=zone_bindings,
            zone_binding_sha256=zone_binding_sha256,
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
        "source_runs": {},
        "source_run_metadata_sha256": None,
        "next_required_evidence": None,
        "release_gate_complete": False,
        "operational_zone_binding_verified": False,
        "zone_binding_sha256": {},
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
