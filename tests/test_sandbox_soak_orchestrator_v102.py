from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest

from app.runtime.sandbox_soak_orchestrator_v102 import (
    CampaignEventType,
    CampaignState,
    FileCampaignEventStoreV102,
    FileEvidenceArchiveV102,
    FileLeaseStoreV102,
    QualificationRunEvidenceV102,
    RunOutcome,
    SoakBlocked,
    SoakCampaignPlanV102,
    SoakCampaignServiceV102,
    SoakCorruption,
    SoakError,
    SoakNotDue,
    StaleSoakLease,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class KillStatus:
    def __init__(self, engaged: bool = False) -> None:
        self.engaged = engaged


class KillSwitch:
    def __init__(self, engaged: bool = False) -> None:
        self._status = KillStatus(engaged)

    def status(self) -> KillStatus:
        return self._status

    def engage(self) -> None:
        self._status.engaged = True


def plan(**changes) -> SoakCampaignPlanV102:
    value = SoakCampaignPlanV102(
        campaign_id="soak-001",
        generation=1,
        starts_at=BASE,
        ends_at=BASE + timedelta(hours=2),
        interval=timedelta(minutes=10),
        schedule_grace=timedelta(minutes=2),
        lease_ttl=timedelta(minutes=1),
        evidence_max_age=timedelta(seconds=30),
        evidence_retention=timedelta(days=90),
        maximum_runs=3,
        minimum_verified_runs=2,
        maximum_total_failures=1,
        maximum_consecutive_failures=1,
    )
    return replace(value, **changes).sealed()


def service(tmp_path: Path, *, campaign_plan=None, kill=None):
    campaign_plan = plan() if campaign_plan is None else campaign_plan
    kill = KillSwitch() if kill is None else kill
    lease_store = FileLeaseStoreV102(tmp_path / "lease.json")
    event_store = FileCampaignEventStoreV102(tmp_path / "campaign.jsonl")
    archive = FileEvidenceArchiveV102(tmp_path / "evidence")
    svc = SoakCampaignServiceV102(
        plan=campaign_plan,
        event_store=event_store,
        lease_store=lease_store,
        evidence_archive=archive,
        kill_switch=kill,
    )
    lease = lease_store.acquire(
        owner_id="worker-a",
        generation=campaign_plan.generation,
        now=BASE - timedelta(seconds=10),
        ttl=campaign_plan.lease_ttl,
    )
    return svc, lease_store, lease, event_store, archive, kill


def evidence(claim, *, outcome=RunOutcome.VERIFIED_CLEAN, captured_at=None, **changes):
    clean = outcome is RunOutcome.VERIFIED_CLEAN
    value = QualificationRunEvidenceV102(
        campaign_id=claim.campaign_id,
        run_id=claim.run_id,
        run_index=claim.run_index,
        generation=claim.generation,
        qualification_id=f"qual-{claim.run_index}",
        captured_at=claim.claimed_at if captured_at is None else captured_at,
        outcome=outcome,
        reasons=(),
        qualification_tail_digest="a" * 64,
        read_only_probe_verified=clean,
        paper_round_trip_verified=clean,
        cleanup_verified=clean,
        kill_switch_engaged=False,
    )
    return replace(value, **changes).sealed()


def start(svc, lease):
    return svc.start(now=BASE, owner_id=lease.owner_id, fencing_token=lease.fencing_token)


def claim(svc, lease, at=BASE):
    return svc.claim_due_run(now=at, owner_id=lease.owner_id, fencing_token=lease.fencing_token)


def record(svc, lease, value, at=None):
    when = value.captured_at if at is None else at
    return svc.record_evidence(
        value,
        now=when,
        owner_id=lease.owner_id,
        fencing_token=lease.fencing_token,
    )


def test_plan_seal_and_tamper_detection():
    sealed = plan()
    sealed.validate()
    assert len(sealed.plan_digest) == 64
    with pytest.raises(SoakCorruption):
        replace(sealed, maximum_runs=4).validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"campaign_id": ""},
        {"generation": 0},
        {"ends_at": BASE},
        {"interval": timedelta(0)},
        {"schedule_grace": timedelta(minutes=10)},
        {"evidence_retention": timedelta(seconds=1)},
        {"maximum_runs": 0},
        {"minimum_verified_runs": 4},
        {"maximum_total_failures": -1},
        {"live_trading_allowed": True},
    ],
)
def test_plan_rejects_invalid_values(changes):
    with pytest.raises((ValueError, SoakCorruption)):
        replace(plan(), plan_digest="", **changes).sealed()


def test_lease_acquire_renew_release_and_fencing(tmp_path):
    store = FileLeaseStoreV102(tmp_path / "lease.json")
    first = store.acquire(owner_id="a", generation=1, now=BASE, ttl=timedelta(seconds=10))
    assert first.fencing_token == 1
    renewed = store.renew(
        owner_id="a", generation=1, fencing_token=1,
        now=BASE + timedelta(seconds=1), ttl=timedelta(seconds=10),
    )
    assert renewed.expires_at == BASE + timedelta(seconds=11)
    with pytest.raises(StaleSoakLease):
        store.acquire(owner_id="b", generation=1, now=BASE + timedelta(seconds=2), ttl=timedelta(seconds=10))
    released = store.release(
        owner_id="a", generation=1, fencing_token=1, now=BASE + timedelta(seconds=3)
    )
    assert released.released
    second = store.acquire(owner_id="b", generation=1, now=BASE + timedelta(seconds=4), ttl=timedelta(seconds=10))
    assert second.fencing_token == 2
    with pytest.raises(StaleSoakLease):
        store.assert_held(owner_id="a", generation=1, fencing_token=1, now=BASE + timedelta(seconds=5))


def test_lease_expiry_allows_new_fencing_token(tmp_path):
    store = FileLeaseStoreV102(tmp_path / "lease.json")
    first = store.acquire(owner_id="a", generation=1, now=BASE, ttl=timedelta(seconds=1))
    second = store.acquire(owner_id="b", generation=1, now=BASE + timedelta(seconds=2), ttl=timedelta(seconds=5))
    assert second.fencing_token == first.fencing_token + 1


def test_lease_tampering_is_detected(tmp_path):
    store = FileLeaseStoreV102(tmp_path / "lease.json")
    store.acquire(owner_id="a", generation=1, now=BASE, ttl=timedelta(seconds=5))
    raw = json.loads(store.path.read_text())
    raw["owner_id"] = "attacker"
    store.path.write_text(json.dumps(raw))
    with pytest.raises(SoakCorruption):
        store.load()


def test_start_requires_current_lease(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    with pytest.raises(StaleSoakLease):
        svc.start(now=BASE, owner_id="other", fencing_token=lease.fencing_token)


def test_start_and_not_due(tmp_path):
    campaign_plan = plan(starts_at=BASE + timedelta(minutes=1), ends_at=BASE + timedelta(hours=2))
    svc, _, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    svc.start(now=BASE, owner_id=lease.owner_id, fencing_token=lease.fencing_token)
    with pytest.raises(SoakNotDue):
        claim(svc, lease, BASE)


def test_clean_runs_complete_campaign(tmp_path):
    svc, lease_store, lease, _, archive, _ = service(tmp_path)
    start(svc, lease)
    for index in range(1, 4):
        now = BASE + timedelta(minutes=10 * (index - 1))
        lease = lease_store.acquire(
            owner_id=lease.owner_id, generation=1, now=now, ttl=timedelta(minutes=1)
        )
        current = claim(svc, lease, now)
        assert current is not None and current.run_index == index
        snapshot = record(svc, lease, evidence(current, captured_at=now), now)
    assert snapshot.state is CampaignState.COMPLETED
    assert snapshot.verified_runs == 3
    assert snapshot.eligible_for_extended_paper_soak
    assert archive.verify()
    assert len(archive.load_manifest()) == 3


def test_one_preflight_failure_can_recover_then_complete(tmp_path):
    svc, lease_store, lease, *_ = service(tmp_path)
    start(svc, lease)
    first = claim(svc, lease, BASE)
    failed = evidence(
        first,
        outcome=RunOutcome.PREFLIGHT_BLOCKED,
        read_only_probe_verified=False,
        paper_round_trip_verified=False,
        cleanup_verified=False,
        reasons=("BROKER_ACCOUNT_NOT_ACTIVE",),
    )
    snapshot = record(svc, lease, failed, BASE)
    assert snapshot.state is CampaignState.ACTIVE
    assert snapshot.total_failures == 1
    for index in (2, 3):
        now = BASE + timedelta(minutes=10 * (index - 1))
        lease = lease_store.acquire(
            owner_id=lease.owner_id, generation=1, now=now, ttl=timedelta(minutes=1)
        )
        current = claim(svc, lease, now)
        snapshot = record(svc, lease, evidence(current, captured_at=now), now)
    assert snapshot.state is CampaignState.COMPLETED
    assert snapshot.verified_runs == 2
    assert snapshot.total_failures == 1


def test_failure_budget_blocks_campaign(tmp_path):
    campaign_plan = plan(maximum_total_failures=0, maximum_consecutive_failures=0)
    svc, _, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    start(svc, lease)
    current = claim(svc, lease)
    failed = evidence(
        current,
        outcome=RunOutcome.PREFLIGHT_BLOCKED,
        read_only_probe_verified=False,
        paper_round_trip_verified=False,
        cleanup_verified=False,
        reasons=("STREAM_NOT_READY",),
    )
    snapshot = record(svc, lease, failed)
    assert snapshot.state is CampaignState.BLOCKED
    assert not snapshot.eligible_for_extended_paper_soak


def test_missed_window_counts_failure_and_can_block(tmp_path):
    campaign_plan = plan(maximum_total_failures=0, maximum_consecutive_failures=0)
    svc, lease_store, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    start(svc, lease)
    lease = lease_store.acquire(owner_id=lease.owner_id, generation=1, now=BASE + timedelta(minutes=3), ttl=timedelta(minutes=1))
    result = claim(svc, lease, BASE + timedelta(minutes=3))
    assert result is None
    assert svc.snapshot().state is CampaignState.BLOCKED
    events = svc.event_store.load()
    assert any(event.event_type is CampaignEventType.RUN_MISSED for event in events)


def test_residual_exposure_blocks_immediately(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    residual = evidence(
        current,
        outcome=RunOutcome.RESIDUAL_PAPER_EXPOSURE,
        reasons=("RESIDUAL_PAPER_EXPOSURE",),
        read_only_probe_verified=True,
        paper_round_trip_verified=False,
        cleanup_verified=False,
        kill_switch_engaged=True,
    )
    snapshot = record(svc, lease, residual)
    assert snapshot.state is CampaignState.BLOCKED


def test_recovery_required_blocks_immediately(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    recovering = evidence(
        current,
        outcome=RunOutcome.RECOVERY_REQUIRED,
        reasons=("AMBIGUOUS_MUTATION",),
        read_only_probe_verified=True,
        paper_round_trip_verified=False,
        cleanup_verified=False,
        kill_switch_engaged=True,
    )
    assert record(svc, lease, recovering).state is CampaignState.BLOCKED


def test_upstream_quarantine_quarantines_campaign(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    quarantined = evidence(
        current,
        outcome=RunOutcome.QUARANTINED,
        reasons=("BROKER_TIME_REGRESSION",),
        read_only_probe_verified=False,
        paper_round_trip_verified=False,
        cleanup_verified=False,
        kill_switch_engaged=True,
    )
    assert record(svc, lease, quarantined).state is CampaignState.QUARANTINED


def test_stale_evidence_is_counted_as_failure(tmp_path):
    svc, lease_store, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    old = evidence(current, captured_at=BASE)
    lease = lease_store.acquire(owner_id=lease.owner_id, generation=1, now=BASE + timedelta(minutes=1), ttl=timedelta(minutes=1))
    snapshot = record(svc, lease, old, BASE + timedelta(minutes=1))
    assert snapshot.total_failures == 1


def test_future_evidence_quarantines(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    future = evidence(current, captured_at=BASE + timedelta(seconds=2))
    assert record(svc, lease, future, BASE).state is CampaignState.QUARANTINED


def test_identity_mismatch_quarantines(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    bad = replace(evidence(current), campaign_id="other", evidence_digest="").sealed()
    assert record(svc, lease, bad).state is CampaignState.QUARANTINED


def test_unsealed_evidence_quarantines(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    bad = replace(evidence(current), evidence_digest="")
    assert record(svc, lease, bad).state is CampaignState.QUARANTINED


def test_kill_switch_prevents_new_run(tmp_path):
    kill = KillSwitch(True)
    svc, _, lease, *_ = service(tmp_path, kill=kill)
    start(svc, lease)
    assert claim(svc, lease) is None
    assert svc.snapshot().state is CampaignState.BLOCKED


def test_overlapping_run_is_rejected(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    assert claim(svc, lease) is not None
    with pytest.raises(SoakError):
        claim(svc, lease)


def test_campaign_replay_restores_counts(tmp_path):
    svc, _, lease, event_store, archive, kill = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    record(svc, lease, evidence(current))
    restored = SoakCampaignServiceV102(
        plan=svc.plan,
        event_store=event_store,
        lease_store=svc.lease_store,
        evidence_archive=archive,
        kill_switch=kill,
    )
    snapshot = restored.snapshot()
    assert snapshot.completed_runs == 1
    assert snapshot.verified_runs == 1
    assert snapshot.state is CampaignState.ACTIVE


def test_journal_tampering_is_detected(tmp_path):
    svc, _, lease, store, *_ = service(tmp_path)
    start(svc, lease)
    lines = store.path.read_text().splitlines()
    raw = json.loads(lines[0])
    raw["to_state"] = "COMPLETED"
    store.path.write_text(json.dumps(raw) + "\n")
    assert not store.verify()
    with pytest.raises(SoakCorruption):
        store.load()


def test_evidence_artifact_tampering_is_detected(tmp_path):
    svc, _, lease, _, archive, _ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    record(svc, lease, evidence(current))
    record_item = archive.load_manifest()[0]
    (archive.root / record_item.relative_path).write_text("tampered")
    assert not archive.verify()


def test_evidence_overwrite_conflict_is_detected(tmp_path):
    archive = FileEvidenceArchiveV102(tmp_path / "archive")
    svc, _, lease, *_ = service(tmp_path / "svc")
    start(svc, lease)
    current = claim(svc, lease)
    value = evidence(current)
    archive.save(value, stored_at=BASE, retention=timedelta(days=1))
    artifact = archive.root / current.campaign_id / f"{current.run_id}.json"
    artifact.write_text("conflict")
    with pytest.raises(SoakCorruption):
        archive.save(value, stored_at=BASE, retention=timedelta(days=1))


def test_minimum_verified_runs_not_met_blocks_at_end(tmp_path):
    campaign_plan = plan(maximum_runs=2, minimum_verified_runs=2, maximum_total_failures=2, maximum_consecutive_failures=2)
    svc, lease_store, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    start(svc, lease)
    first = claim(svc, lease)
    failed = evidence(
        first,
        outcome=RunOutcome.PREFLIGHT_BLOCKED,
        reasons=("READ_FAILURE",),
        read_only_probe_verified=False,
        paper_round_trip_verified=False,
        cleanup_verified=False,
    )
    record(svc, lease, failed)
    now = BASE + timedelta(minutes=10)
    lease = lease_store.acquire(owner_id="worker-a", generation=1, now=now, ttl=timedelta(minutes=1))
    second = claim(svc, lease, now)
    snapshot = record(svc, lease, evidence(second, captured_at=now), now)
    assert snapshot.state is CampaignState.BLOCKED
    assert snapshot.verified_runs == 1


def test_terminal_campaign_rejects_new_run(tmp_path):
    campaign_plan = plan(maximum_runs=1, minimum_verified_runs=1)
    svc, _, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    start(svc, lease)
    current = claim(svc, lease)
    assert record(svc, lease, evidence(current)).state is CampaignState.COMPLETED
    with pytest.raises(SoakError):
        claim(svc, lease)


def test_concurrent_lease_acquisition_has_single_owner(tmp_path):
    store = FileLeaseStoreV102(tmp_path / "lease.json")
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(owner: str) -> None:
        try:
            result = store.acquire(owner_id=owner, generation=1, now=BASE, ttl=timedelta(seconds=10))
            value = f"ok:{result.owner_id}"
        except StaleSoakLease:
            value = f"blocked:{owner}"
        with lock:
            outcomes.append(value)

    threads = [threading.Thread(target=worker, args=(f"owner-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item.startswith("ok:") for item in outcomes) == 1
    assert sum(item.startswith("blocked:") for item in outcomes) == 7


def test_active_lease_reacquire_by_same_owner_renews_without_new_token(tmp_path):
    store = FileLeaseStoreV102(tmp_path / "lease.json")
    first = store.acquire(owner_id="a", generation=1, now=BASE, ttl=timedelta(seconds=10))
    second = store.acquire(owner_id="a", generation=1, now=BASE + timedelta(seconds=1), ttl=timedelta(seconds=20))
    assert second.fencing_token == first.fencing_token
    assert second.expires_at == BASE + timedelta(seconds=21)


def test_start_after_campaign_end_is_blocked(tmp_path):
    campaign_plan = plan(starts_at=BASE - timedelta(hours=2), ends_at=BASE - timedelta(hours=1))
    svc, lease_store, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    lease = lease_store.acquire(owner_id="worker-a", generation=1, now=BASE, ttl=timedelta(minutes=1))
    with pytest.raises(SoakBlocked):
        svc.start(now=BASE, owner_id=lease.owner_id, fencing_token=lease.fencing_token)


def test_run_claim_digest_detects_tampering(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    current.validate()
    with pytest.raises(SoakCorruption):
        replace(current, run_index=2).validate()


def test_evidence_digest_detects_tampering(tmp_path):
    svc, _, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    value = evidence(current)
    with pytest.raises(SoakCorruption):
        replace(value, qualification_id="changed").validate()


def test_restart_with_active_run_can_finish_using_same_lease(tmp_path):
    svc, _, lease, event_store, archive, kill = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    restored = SoakCampaignServiceV102(
        plan=svc.plan,
        event_store=event_store,
        lease_store=svc.lease_store,
        evidence_archive=archive,
        kill_switch=kill,
    )
    assert restored.snapshot().state is CampaignState.RUNNING
    assert restored.snapshot().active_run_id == current.run_id
    snapshot = record(restored, lease, evidence(current))
    assert snapshot.completed_runs == 1
    assert snapshot.state is CampaignState.ACTIVE


def test_released_lease_cannot_finish_active_run(tmp_path):
    svc, lease_store, lease, *_ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    lease_store.release(
        owner_id=lease.owner_id,
        generation=lease.generation,
        fencing_token=lease.fencing_token,
        now=BASE + timedelta(seconds=1),
    )
    with pytest.raises(StaleSoakLease):
        record(svc, lease, evidence(current, captured_at=BASE + timedelta(seconds=1)), BASE + timedelta(seconds=1))


def test_consecutive_failure_budget_resets_after_clean_run(tmp_path):
    campaign_plan = plan(
        maximum_runs=3,
        minimum_verified_runs=1,
        maximum_total_failures=2,
        maximum_consecutive_failures=1,
    )
    svc, lease_store, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    start(svc, lease)
    first = claim(svc, lease)
    record(
        svc,
        lease,
        evidence(
            first,
            outcome=RunOutcome.PREFLIGHT_BLOCKED,
            reasons=("READ_FAILURE",),
            read_only_probe_verified=False,
            paper_round_trip_verified=False,
            cleanup_verified=False,
        ),
    )
    now = BASE + timedelta(minutes=10)
    lease = lease_store.acquire(owner_id="worker-a", generation=1, now=now, ttl=timedelta(minutes=1))
    second = claim(svc, lease, now)
    clean = record(svc, lease, evidence(second, captured_at=now), now)
    assert clean.consecutive_failures == 0
    now = BASE + timedelta(minutes=20)
    lease = lease_store.acquire(owner_id="worker-a", generation=1, now=now, ttl=timedelta(minutes=1))
    third = claim(svc, lease, now)
    final = record(
        svc,
        lease,
        evidence(
            third,
            captured_at=now,
            outcome=RunOutcome.PREFLIGHT_BLOCKED,
            reasons=("STREAM_NOT_READY",),
            read_only_probe_verified=False,
            paper_round_trip_verified=False,
            cleanup_verified=False,
        ),
        now,
    )
    assert final.state is CampaignState.COMPLETED
    assert final.total_failures == 2
    assert final.verified_runs == 1


def test_manifest_record_has_retention_deadline(tmp_path):
    svc, _, lease, _, archive, _ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    record(svc, lease, evidence(current))
    manifest = archive.load_manifest()[0]
    assert manifest.retain_until == BASE + svc.plan.evidence_retention


def test_manifest_record_tampering_is_detected(tmp_path):
    svc, _, lease, _, archive, _ = service(tmp_path)
    start(svc, lease)
    current = claim(svc, lease)
    record(svc, lease, evidence(current))
    raw = json.loads(archive.manifest_path.read_text())
    raw["retain_until"] = (BASE + timedelta(days=1)).isoformat()
    archive.manifest_path.write_text(json.dumps(raw) + "\n")
    with pytest.raises(SoakCorruption):
        archive.load_manifest()


def test_journal_generation_mismatch_is_detected_on_service_restore(tmp_path):
    svc, _, lease, store, archive, kill = service(tmp_path)
    start(svc, lease)
    lines = store.path.read_text().splitlines()
    raw = json.loads(lines[0])
    raw["generation"] = 2
    base = {key: raw[key] for key in (
        "sequence", "campaign_id", "event_type", "from_state", "to_state",
        "occurred_at", "generation", "attributes", "previous_digest"
    )}
    from app.runtime.sandbox_soak_orchestrator_v102 import digest as compute_digest
    raw["event_digest"] = compute_digest(base)
    store.path.write_text(json.dumps(raw) + "\n")
    with pytest.raises(SoakCorruption):
        SoakCampaignServiceV102(
            plan=svc.plan,
            event_store=store,
            lease_store=svc.lease_store,
            evidence_archive=archive,
            kill_switch=kill,
        )


def test_blocked_campaign_does_not_auto_resume_after_new_lease(tmp_path):
    campaign_plan = plan(maximum_total_failures=0, maximum_consecutive_failures=0)
    svc, lease_store, lease, *_ = service(tmp_path, campaign_plan=campaign_plan)
    start(svc, lease)
    current = claim(svc, lease)
    snapshot = record(
        svc,
        lease,
        evidence(
            current,
            outcome=RunOutcome.PREFLIGHT_BLOCKED,
            reasons=("READ_FAILURE",),
            read_only_probe_verified=False,
            paper_round_trip_verified=False,
            cleanup_verified=False,
        ),
    )
    assert snapshot.state is CampaignState.BLOCKED
    new_lease = lease_store.acquire(
        owner_id="worker-b",
        generation=1,
        now=BASE + timedelta(minutes=2),
        ttl=timedelta(minutes=1),
    )
    with pytest.raises(SoakError):
        svc.claim_due_run(
            now=BASE + timedelta(minutes=2),
            owner_id=new_lease.owner_id,
            fencing_token=new_lease.fencing_token,
        )


def test_snapshot_tail_digest_matches_journal(tmp_path):
    svc, _, lease, store, *_ = service(tmp_path)
    start(svc, lease)
    assert svc.snapshot().tail_digest == store.load()[-1].event_digest
