from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from app.runtime.remote_signer_attestation_v109 import (
    InMemoryRemoteSignerRepositoryV109,
    RemoteSignerConflictV109,
    RemoteSignerPolicySnapshotV109,
    RemoteSignRequestV109,
    VerifiedRemoteSignerPolicyV109,
    bytes_digest_v109,
)

UTC = UTC


def _policy(
    *, generation: int = 1, endpoint: str = "https://signer.example.test"
) -> VerifiedRemoteSignerPolicyV109:
    now = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    snapshot = RemoteSignerPolicySnapshotV109(
        provider_id="provider-1",
        generation=generation,
        endpoint_origin=endpoint,
        mtls_identity_ref="workload-cert-1",
        signing_key_id="signing-key-1",
        signing_public_key_b64=base64.b64encode(b"s" * 32).decode("ascii"),
        attestation_key_id="attestation-key-1",
        attestation_public_key_b64=base64.b64encode(b"a" * 32).decode("ascii"),
        allowed_hardware_clusters=("cluster-a",),
        allowed_firmware_measurements=("f" * 64,),
        predecessor_keyring_digest="1" * 64,
        request_ttl_seconds=60,
        timeout_seconds=5.0,
        max_response_bytes=65536,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        root_key_id="root-key-1",
        root_signature_b64=base64.b64encode(b"r" * 64).decode("ascii"),
    )
    return VerifiedRemoteSignerPolicyV109(snapshot=snapshot, verified_at=now)


def _request(
    index: int, policy: VerifiedRemoteSignerPolicyV109
) -> tuple[RemoteSignRequestV109, bytes]:
    now = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    payload = f"stress-payload-{index}".encode()
    request = RemoteSignRequestV109(
        request_id=f"request-{index}",
        nonce=f"nonce-{index}",
        provider_id=policy.provider_id,
        policy_generation=policy.generation,
        policy_digest=policy.policy_digest,
        key_id=policy.snapshot.signing_key_id,
        key_generation=1,
        keyring_generation=1,
        purpose="CONTROLLER_COMMAND",
        domain="astra.remote-signer.stress.v109",
        payload_digest=bytes_digest_v109(payload),
        created_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    return request, payload


def run_stress(*, iterations: int = 1000, workers: int = 8) -> dict[str, object]:
    if iterations <= 0 or workers <= 0:
        raise ValueError("iterations and workers must be positive")

    repository = InMemoryRemoteSignerRepositoryV109()
    policy = _policy()
    repository.install_verified_policy(policy)
    failures: list[str] = []

    def create_and_claim(index: int) -> None:
        try:
            request, payload = _request(index, policy)
            repository.create_request_with_outbox(request, payload)
            repository.mark_dispatch_started(
                request.request_id,
                worker_id=f"worker-{index % workers}",
                observed_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            )
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(f"{index}:{type(exc).__name__}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(create_and_claim, range(iterations)))

    duplicate_dispatch_rejected = False
    try:
        repository.mark_dispatch_started(
            "request-0",
            worker_id="worker-replay",
            observed_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
        )
    except RemoteSignerConflictV109:
        duplicate_dispatch_rejected = True

    replay_rejected = False
    request_zero, payload_zero = _request(0, policy)
    try:
        repository.create_request_with_outbox(request_zero, payload_zero)
    except RemoteSignerConflictV109:
        replay_rejected = True

    equivocation_rejected = False
    conflicting = _policy(generation=1, endpoint="https://signer-alt.example.test")
    try:
        repository.install_verified_policy(conflicting)
    except RemoteSignerConflictV109:
        equivocation_rejected = True

    result: dict[str, object] = {
        "schema": 109,
        "iterations": iterations,
        "workers": workers,
        "failures": len(failures),
        "failure_samples": failures[:10],
        "duplicate_dispatch_rejected": duplicate_dispatch_rejected,
        "request_replay_rejected": replay_rejected,
        "policy_equivocation_rejected": equivocation_rejected,
    }
    result["status"] = (
        "PASS"
        if not failures
        and duplicate_dispatch_rejected
        and replay_rejected
        and equivocation_rejected
        else "FAIL"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-stress-v109")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    result = run_stress(iterations=args.iterations, workers=args.workers)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
