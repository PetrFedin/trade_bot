from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from typing import Sequence

from app.runtime.campaign_control_plane_v103 import (
    ControlPlanePolicyV103,
    InMemoryControlPlaneStoreV103,
    ReadOnlyCampaignControlPlaneV103,
    ReadOnlyProbePlanV103,
    WorkerHeartbeatV103,
    build_verified_probe_evidence,
    deterministic_evidence_bytes,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _one(index: int) -> str:
    campaign_id = f"stress-{index:06d}"
    store = InMemoryControlPlaneStoreV103()
    policy = ControlPlanePolicyV103(
        campaign_id=campaign_id,
        generation=1,
        starts_at=BASE,
        ends_at=BASE + timedelta(days=1),
        run_interval=timedelta(hours=1),
        lease_ttl=timedelta(minutes=5),
        heartbeat_ttl=timedelta(minutes=2),
        probe_timeout=timedelta(seconds=30),
        evidence_chunk_bytes=256,
        evidence_retention=timedelta(days=7),
        allowed_read_only_hosts=("paper-api.alpaca.markets",),
    )
    store.register_campaign(policy, BASE)
    receipt = store.acquire_lease(campaign_id, f"worker-{index}", 1, BASE)
    store.heartbeat(
        campaign_id,
        WorkerHeartbeatV103(
            owner_id=receipt.owner_id,
            generation=receipt.generation,
            fencing_token=receipt.fencing_token,
            observed_at=BASE,
            deployment_id="stress-deployment",
            build_identity="schema103",
        ),
    )
    plan = ReadOnlyProbePlanV103(
        run_id=f"run-{index}",
        request_id=f"request-{index}",
        campaign_id=campaign_id,
        generation=1,
        account_id="paper-account",
        host="paper-api.alpaca.markets",
        method="GET",
        path="/v2/account",
        created_at=BASE,
        deadline_at=BASE + timedelta(seconds=20),
    )
    evidence = build_verified_probe_evidence(plan, BASE, {"index": index})
    snapshot = ReadOnlyCampaignControlPlaneV103(store).run_read_only_probe(
        plan,
        receipt,
        evidence,
        deterministic_evidence_bytes(evidence),
        f"upload-{index}",
        BASE,
    )
    if snapshot.external_order_routing_allowed or snapshot.live_trading_allowed:
        raise AssertionError("routing boundary violated")
    if not store.verify_event_chain(campaign_id):
        raise AssertionError("event chain invalid")
    return snapshot.event_tail_digest


def run(iterations: int, workers: int) -> dict[str, object]:
    if iterations < 1 or workers < 1:
        raise ValueError("iterations and workers must be positive")
    failures: list[str] = []
    digests: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_one, index) for index in range(iterations)]
        for future in futures:
            try:
                digests.append(future.result())
            except Exception as exc:  # pragma: no cover - reported in result
                failures.append(f"{type(exc).__name__}:{exc}")
    return {
        "schema": 103,
        "iterations": iterations,
        "workers": workers,
        "successes": len(digests),
        "failures": len(failures),
        "failure_samples": failures[:10],
        "unique_tail_digests": len(set(digests)),
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    result = run(args.iterations, args.workers)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["failures"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
