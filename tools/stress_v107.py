from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime.rollout_execution_v107 import (
    ApprovalAttestationV107,
    ApprovalRoleV107,
    DeploymentActionV107,
    DeploymentExecutionIntentV107,
    DeploymentExecutionPolicyV107,
    ExecutionReplayLedgerV107,
    SignedDeploymentExecutionCommandV107,
)

UTC = timezone.utc
IMAGE = "sha256:" + "1" * 64
CONFIG = "sha256:" + "2" * 64
RELEASE_SECRET = b"r" * 32
RISK_SECRET = b"k" * 32
CONTROLLER_SECRET = b"c" * 32


def _command(index: int, now: datetime) -> SignedDeploymentExecutionCommandV107:
    intent = DeploymentExecutionIntentV107(
        command_id=f"stress-command-{index}",
        action_id=f"stress-action-{index}",
        qualification_id=f"stress-qualification-{index}",
        qualification_action_digest=f"{index + 2:064x}"[-64:],
        action=DeploymentActionV107.PROMOTE,
        cluster="cluster-a",
        namespace="astra-prod",
        deployment_name="trade-bot-workers",
        deployment_uid="uid-123",
        service_account="astra-worker",
        expected_resource_version=str(1000 + index),
        expected_generation=7,
        expected_current_replicas=2,
        target_replicas=4,
        expected_image_digest=IMAGE,
        expected_config_digest=CONFIG,
        qualification_evidence_digest=f"{index:064x}"[-64:],
        qualification_state_digest=f"{index + 1:064x}"[-64:],
        issued_at=now,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        idempotency_key=f"stress-idempotency-{index}",
        fencing_token=index + 1,
        nonce=f"stress-nonce-{index}",
    )
    release = ApprovalAttestationV107.sign(
        approval_id=f"stress-release-{index}",
        intent=intent,
        approver_id=f"release-approver-{index}",
        role=ApprovalRoleV107.RELEASE,
        signed_at=now,
        nonce=f"stress-release-nonce-{index}",
        key_id="release-key",
        secret=RELEASE_SECRET,
    )
    risk = ApprovalAttestationV107.sign(
        approval_id=f"stress-risk-{index}",
        intent=intent,
        approver_id=f"risk-approver-{index}",
        role=ApprovalRoleV107.RISK,
        signed_at=now,
        nonce=f"stress-risk-nonce-{index}",
        key_id="risk-key",
        secret=RISK_SECRET,
    )
    return SignedDeploymentExecutionCommandV107.sign(
        intent=intent,
        approvals=(release, risk),
        controller_key_id="controller-key",
        controller_secret=CONTROLLER_SECRET,
    )


def stress(*, iterations: int = 1_000, workers: int = 8) -> dict[str, object]:
    if iterations <= 0 or workers <= 0:
        raise ValueError("iterations and workers must be positive")
    policy = DeploymentExecutionPolicyV107(
        cluster="cluster-a",
        namespace="astra-prod",
        deployment_name="trade-bot-workers",
        deployment_uid="uid-123",
        service_account="astra-worker",
        expected_image_digest=IMAGE,
        expected_config_digest=CONFIG,
        min_replicas=2,
        max_replicas=10,
        rollback_replicas=1,
    )
    ledger = ExecutionReplayLedgerV107()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    failures: list[str] = []
    digests: set[str] = set()

    def run(index: int) -> str:
        command = _command(index, now)
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=now,
            replay_ledger=ledger,
        )
        return command.command_digest

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, index): index for index in range(iterations)}
        for future in as_completed(futures):
            try:
                digests.add(future.result())
            except Exception as exc:
                failures.append(f"{futures[future]}:{type(exc).__name__}:{exc}")

    return {
        "schema": 107,
        "status": "PASS" if not failures and len(ledger) == iterations and len(digests) == iterations else "FAIL",
        "iterations": iterations,
        "workers": workers,
        "failures": failures,
        "replay_ledger_size": len(ledger),
        "unique_command_digests": len(digests),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-stress-v107")
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    result = stress(iterations=args.iterations, workers=args.workers)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
