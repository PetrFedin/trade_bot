from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from typing import Sequence

from app.runtime.sandbox_soak_orchestrator_v102 import (
    CampaignState,
    FileCampaignEventStoreV102,
    FileEvidenceArchiveV102,
    FileLeaseStoreV102,
    QualificationRunEvidenceV102,
    RunOutcome,
    SoakCampaignPlanV102,
    SoakCampaignServiceV102,
)

UTC = timezone.utc


class _KillStatus:
    engaged = False


class _KillSwitch:
    def status(self) -> _KillStatus:
        return _KillStatus()


def _run(index: int, root: Path) -> bool:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC) + timedelta(microseconds=index)
    campaign_id = f"stress-{index:06d}"
    plan = SoakCampaignPlanV102(
        campaign_id=campaign_id,
        generation=1,
        starts_at=now,
        ends_at=now + timedelta(minutes=5),
        interval=timedelta(minutes=1),
        schedule_grace=timedelta(seconds=30),
        lease_ttl=timedelta(minutes=1),
        evidence_max_age=timedelta(seconds=30),
        evidence_retention=timedelta(days=1),
        maximum_runs=1,
        minimum_verified_runs=1,
        maximum_total_failures=0,
        maximum_consecutive_failures=0,
    ).sealed()
    work = root / campaign_id
    lease_store = FileLeaseStoreV102(work / "lease.json")
    event_store = FileCampaignEventStoreV102(work / "events.jsonl")
    archive = FileEvidenceArchiveV102(work / "evidence")
    service = SoakCampaignServiceV102(
        plan=plan,
        event_store=event_store,
        lease_store=lease_store,
        evidence_archive=archive,
        kill_switch=_KillSwitch(),
    )
    lease = lease_store.acquire(owner_id="stress-worker", generation=1, now=now, ttl=timedelta(minutes=1))
    service.start(now=now, owner_id=lease.owner_id, fencing_token=lease.fencing_token)
    claim = service.claim_due_run(now=now, owner_id=lease.owner_id, fencing_token=lease.fencing_token)
    if claim is None:
        return False
    evidence = QualificationRunEvidenceV102(
        campaign_id=campaign_id,
        run_id=claim.run_id,
        run_index=1,
        generation=1,
        qualification_id=f"qualification-{index}",
        captured_at=now,
        outcome=RunOutcome.VERIFIED_CLEAN,
        reasons=(),
        qualification_tail_digest="b" * 64,
        read_only_probe_verified=True,
        paper_round_trip_verified=True,
        cleanup_verified=True,
        kill_switch_engaged=False,
    ).sealed()
    snapshot = service.record_evidence(
        evidence,
        now=now,
        owner_id=lease.owner_id,
        fencing_token=lease.fencing_token,
    )
    return (
        snapshot.state is CampaignState.COMPLETED
        and snapshot.eligible_for_extended_paper_soak
        and event_store.verify()
        and archive.verify()
        and not snapshot.external_order_routing_allowed
        and not snapshot.live_trading_allowed
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.workers < 1:
        parser.error("iterations and workers must be positive")
    failures: list[int] = []
    with tempfile.TemporaryDirectory(prefix="astra-v102-stress-") as temporary:
        root = Path(temporary)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_run, index, root): index for index in range(args.iterations)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    if not future.result():
                        failures.append(index)
                except Exception:
                    failures.append(index)
    result = {
        "schema": 102,
        "status": "PASS" if not failures else "FAIL",
        "iterations": args.iterations,
        "workers": args.workers,
        "failures": len(failures),
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
