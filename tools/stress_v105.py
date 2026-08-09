from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from typing import Sequence

from app.runtime.fleet_operations_v105 import (
    FleetEnrollmentAuthorityV105, FleetPolicyV105, FleetRegistryV105,
    KubernetesAttestationV105, RotatingKeyRingV105, SigningKeyV105,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def _policy(index: int) -> FleetPolicyV105:
    return FleetPolicyV105(
        fleet_id=f"fleet-{index}", generation=105, min_replicas=1, max_replicas=5,
        max_scale_up_step=2, max_scale_down_step=1, target_queue_per_worker=2,
        heartbeat_ttl=timedelta(seconds=30), enrollment_ttl=timedelta(minutes=5),
        drain_timeout=timedelta(seconds=20), scale_up_cooldown=timedelta(seconds=5),
        scale_down_cooldown=timedelta(seconds=10), stabilization_samples=2,
        crash_budget=1, dlq_budget=1, allowed_clusters=("cluster",),
        allowed_namespaces=("astra",), allowed_service_accounts=("worker",),
        allowed_zones=("zone-a",), allowed_s3_hosts=("evidence.internal.example",),
        evidence_bucket="astra-evidence", evidence_prefix="fleet",
    )


def _one(index: int) -> str:
    now = BASE + timedelta(microseconds=index)
    policy = _policy(index)
    key = SigningKeyV105("key", b"k" * 32, not_before=BASE - timedelta(1), not_after=BASE + timedelta(days=1))
    ring = RotatingKeyRingV105([key])
    registry = FleetRegistryV105(policy, ring)
    auth = FleetEnrollmentAuthorityV105(policy, ring)
    worker_id = f"worker-{index}"
    attestation = KubernetesAttestationV105(
        cluster="cluster", namespace="astra", service_account="worker",
        pod_uid=f"pod-{index}", node_uid=f"node-{index}", deployment_id="deployment",
        zone="zone-a", image_digest="sha256:" + "1" * 64, config_digest="2" * 64,
        audience="astra-worker-enrollment-v105",
    )
    certificate = f"{index:064x}"[-64:]
    token = auth.issue(token_id=f"token-{index}", worker_id=worker_id, attestation=attestation, certificate_fingerprint=certificate, nonce=f"nonce-{index}", now=now)
    registry.enroll(token, attestation, now)
    registry.heartbeat(worker_id, certificate, 1, now + timedelta(seconds=1))
    registry.assign_claim(worker_id, now + timedelta(seconds=1))
    registry.complete_claim(worker_id, now + timedelta(seconds=2))
    registry.journal.verify()
    return registry.journal.tail_digest


def run(iterations: int, workers: int) -> dict[str, object]:
    failures: list[str] = []
    digests: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_one, index) for index in range(iterations)]
        for future in futures:
            try:
                digests.append(future.result())
            except Exception as exc:  # pragma: no cover
                failures.append(f"{type(exc).__name__}:{exc}")
    return {
        "schema": 105, "iterations": iterations, "workers": workers,
        "failures": len(failures), "failure_samples": failures[:5],
        "unique_tail_digests": len(set(digests)),
        "status": "PASS" if not failures and len(set(digests)) == iterations else "FAIL",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    if args.iterations <= 0 or args.workers <= 0:
        parser.error("iterations and workers must be positive")
    result = run(args.iterations, args.workers)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
