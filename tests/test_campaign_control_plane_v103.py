from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

import pytest

from app.runtime.campaign_control_plane_v103 import (
    CampaignState,
    ControlPlanePolicyV103,
    IncidentSeverity,
    IncidentStatus,
    InMemoryControlPlaneStoreV103,
    IntegrityViolation,
    InvalidTransition,
    LeaseUnavailable,
    MutationNotAllowed,
    ProbeOutcome,
    ReadOnlyCampaignControlPlaneV103,
    ReadOnlyProbeEvidenceV103,
    ReadOnlyProbePlanV103,
    StaleFencingToken,
    StaleGeneration,
    UploadState,
    WorkerHeartbeatV103,
    WorkerHealth,
    build_verified_probe_evidence,
    deterministic_evidence_bytes,
    sha256_hex,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def policy(**changes):
    values = dict(
        campaign_id="campaign-103",
        generation=1,
        starts_at=BASE,
        ends_at=BASE + timedelta(days=3),
        run_interval=timedelta(hours=1),
        lease_ttl=timedelta(minutes=5),
        heartbeat_ttl=timedelta(minutes=2),
        probe_timeout=timedelta(seconds=30),
        evidence_chunk_bytes=256,
        evidence_retention=timedelta(days=7),
        maximum_open_incidents=4,
        allowed_read_only_hosts=("paper-api.alpaca.markets", "data.alpaca.markets"),
    )
    values.update(changes)
    return ControlPlanePolicyV103(**values)


def registered_store(now=BASE):
    store = InMemoryControlPlaneStoreV103()
    store.register_campaign(policy(), now)
    return store


def lease(store, now=BASE, owner="worker-1", generation=1):
    return store.acquire_lease("campaign-103", owner, generation, now)


def heartbeat(store, receipt, now=BASE):
    return store.heartbeat(
        "campaign-103",
        WorkerHeartbeatV103(
            owner_id=receipt.owner_id,
            generation=receipt.generation,
            fencing_token=receipt.fencing_token,
            observed_at=now,
            deployment_id="dep-103",
            build_identity="build-abc",
        ),
    )


def plan(now=BASE, **changes):
    values = dict(
        run_id="run-1",
        request_id="request-1",
        campaign_id="campaign-103",
        generation=1,
        account_id="paper-account",
        host="paper-api.alpaca.markets",
        method="GET",
        path="/v2/account",
        created_at=now,
        deadline_at=now + timedelta(seconds=20),
    )
    values.update(changes)
    return ReadOnlyProbePlanV103(**values)


def verified_evidence(probe_plan, now=BASE):
    return build_verified_probe_evidence(probe_plan, now, {"status": "ACTIVE"})


def start_verified_upload(store, now=BASE, data=b"x" * 600):
    receipt = lease(store, now)
    heartbeat(store, receipt, now)
    probe_plan = plan(now)
    store.begin_read_only_probe(probe_plan, receipt, now)
    store.record_probe_evidence(verified_evidence(probe_plan, now), receipt, now)
    manifest = store.open_evidence_upload(
        "campaign-103",
        "run-1",
        "upload-1",
        len(data),
        sha256_hex(data),
        receipt,
        now,
    )
    return receipt, manifest, data


def upload_all(store, receipt, manifest, data, now=BASE):
    offset = manifest.next_offset
    while offset < len(data):
        chunk = data[offset : offset + manifest.chunk_size]
        manifest = store.upload_evidence_chunk(
            "campaign-103",
            manifest.upload_id,
            offset,
            chunk,
            sha256_hex(chunk),
            receipt,
            now,
        )
        offset = manifest.next_offset
    return manifest


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"campaign_id": ""}, ValueError),
        ({"generation": 0}, ValueError),
        ({"ends_at": BASE}, ValueError),
        ({"heartbeat_ttl": timedelta(minutes=6)}, ValueError),
        ({"evidence_chunk_bytes": 128}, ValueError),
        ({"maximum_open_incidents": 0}, ValueError),
        ({"allowed_read_only_hosts": ()}, ValueError),
        ({"external_order_routing_allowed": True}, MutationNotAllowed),
        ({"live_trading_allowed": True}, MutationNotAllowed),
    ],
)
def test_policy_validation(changes, error):
    with pytest.raises(error):
        policy(**changes)


def test_policy_digest_is_deterministic():
    assert policy().digest == policy().digest
    assert policy(generation=2).digest != policy().digest


def test_registration_is_idempotent_for_same_policy():
    store = registered_store()
    first = store.snapshot("campaign-103", BASE)
    second = store.register_campaign(policy(), BASE)
    assert first.policy_digest == second.policy_digest
    assert len(store.events("campaign-103")) == 1


def test_registration_rejects_different_policy():
    store = registered_store()
    with pytest.raises(IntegrityViolation):
        store.register_campaign(policy(run_interval=timedelta(hours=2)), BASE)


def test_future_campaign_activates_when_due():
    store = InMemoryControlPlaneStoreV103()
    p = policy(starts_at=BASE + timedelta(hours=1), ends_at=BASE + timedelta(days=1))
    snapshot = store.register_campaign(p, BASE)
    assert snapshot.state is CampaignState.CREATED
    assert store.activate_due_campaigns(BASE + timedelta(minutes=59)) == ()
    assert store.activate_due_campaigns(BASE + timedelta(hours=1)) == ("campaign-103",)
    assert store.snapshot("campaign-103", BASE + timedelta(hours=1)).state is CampaignState.READY


def test_lease_acquisition_and_fencing_token():
    store = registered_store()
    receipt = lease(store)
    assert receipt.fencing_token == 1
    assert store.snapshot("campaign-103", BASE).state is CampaignState.LEASED


def test_lease_rejects_wrong_generation():
    store = registered_store()
    with pytest.raises(StaleGeneration):
        lease(store, generation=2)


def test_lease_rejects_not_due_campaign():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    upload_all(store, receipt, manifest, data)
    store.finalize_evidence_upload("campaign-103", "upload-1", receipt, BASE)
    with pytest.raises(LeaseUnavailable):
        lease(store, BASE + timedelta(minutes=30))


def test_lease_retires_campaign_after_window():
    store = registered_store()
    with pytest.raises(LeaseUnavailable):
        lease(store, BASE + timedelta(days=4))
    assert store.snapshot("campaign-103", BASE + timedelta(days=4)).state is CampaignState.RETIRED


def test_heartbeat_extends_lease_and_records_identity():
    store = registered_store()
    receipt = lease(store)
    snapshot = heartbeat(store, receipt, BASE + timedelta(minutes=1))
    assert snapshot.worker_health is WorkerHealth.HEALTHY
    assert snapshot.lease_expires_at == BASE + timedelta(minutes=6)


def test_heartbeat_rejects_stale_token():
    store = registered_store()
    receipt = lease(store)
    bad = replace(receipt, fencing_token=receipt.fencing_token + 1)
    with pytest.raises(StaleFencingToken):
        heartbeat(store, bad)


def test_heartbeat_time_regression_quarantines():
    store = registered_store()
    receipt = lease(store)
    heartbeat(store, receipt, BASE + timedelta(seconds=10))
    with pytest.raises(IntegrityViolation):
        heartbeat(store, receipt, BASE + timedelta(seconds=5))
    assert store.snapshot("campaign-103", BASE).state is CampaignState.QUARANTINED


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_probe_plan_rejects_mutating_methods(method):
    with pytest.raises(MutationNotAllowed):
        plan(method=method)


def test_probe_plan_rejects_mutation_flag():
    with pytest.raises(MutationNotAllowed):
        plan(mutation_requested=True)


def test_probe_rejects_non_allowlisted_host_and_quarantines():
    store = registered_store()
    receipt = lease(store)
    with pytest.raises(MutationNotAllowed):
        store.begin_read_only_probe(plan(host="evil.example"), receipt, BASE)
    assert store.snapshot("campaign-103", BASE).state is CampaignState.QUARANTINED


def test_probe_rejects_deadline_beyond_policy():
    store = registered_store()
    receipt = lease(store)
    with pytest.raises(ValueError):
        store.begin_read_only_probe(plan(deadline_at=BASE + timedelta(minutes=1)), receipt, BASE)


def test_probe_deadline_expiry_blocks_and_opens_incident():
    store = registered_store()
    receipt = lease(store)
    expired = plan(created_at=BASE - timedelta(seconds=25), deadline_at=BASE - timedelta(seconds=1))
    with pytest.raises(InvalidTransition):
        store.begin_read_only_probe(expired, receipt, BASE)
    snapshot = store.snapshot("campaign-103", BASE)
    assert snapshot.state is CampaignState.BLOCKED
    assert snapshot.critical_incidents == 1


def test_verified_probe_moves_to_uploading():
    store = registered_store()
    receipt = lease(store)
    probe_plan = plan()
    store.begin_read_only_probe(probe_plan, receipt, BASE)
    snapshot = store.record_probe_evidence(verified_evidence(probe_plan), receipt, BASE)
    assert snapshot.state is CampaignState.UPLOADING


@pytest.mark.parametrize("outcome", [ProbeOutcome.FAILED, ProbeOutcome.ERROR])
def test_non_verified_probe_blocks(outcome):
    store = registered_store()
    receipt = lease(store)
    probe_plan = plan()
    store.begin_read_only_probe(probe_plan, receipt, BASE)
    evidence = ReadOnlyProbeEvidenceV103(
        run_id=probe_plan.run_id,
        request_id=probe_plan.request_id,
        campaign_id=probe_plan.campaign_id,
        generation=probe_plan.generation,
        account_id=probe_plan.account_id,
        observed_at=BASE,
        outcome=outcome,
        account_read_verified=False,
        open_orders_read_verified=False,
        stream_auth_verified=False,
        mutation_count=0,
        external_order_routing_attempted=False,
        payload_digest="a" * 64,
        diagnostic_code="FAIL",
    )
    snapshot = store.record_probe_evidence(evidence, receipt, BASE)
    assert snapshot.state is CampaignState.BLOCKED
    assert snapshot.critical_incidents == 1


def test_verified_evidence_requires_all_checks():
    with pytest.raises(ValueError):
        ReadOnlyProbeEvidenceV103(
            run_id="r",
            request_id="q",
            campaign_id="c",
            generation=1,
            account_id="a",
            observed_at=BASE,
            outcome=ProbeOutcome.VERIFIED,
            account_read_verified=True,
            open_orders_read_verified=False,
            stream_auth_verified=True,
            mutation_count=0,
            external_order_routing_attempted=False,
            payload_digest="a" * 64,
        )


def test_evidence_rejects_mutation_count():
    probe_plan = plan()
    with pytest.raises(MutationNotAllowed):
        replace(verified_evidence(probe_plan), mutation_count=1)


def test_upload_open_is_idempotent():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    same = store.open_evidence_upload(
        "campaign-103", "run-1", "upload-1", len(data), sha256_hex(data), receipt, BASE
    )
    assert same.manifest_digest == manifest.manifest_digest


def test_upload_identity_collision_quarantines():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    with pytest.raises(IntegrityViolation):
        store.open_evidence_upload(
            "campaign-103", "run-1", "upload-1", len(data) + 1, sha256_hex(data), receipt, BASE
        )
    assert store.snapshot("campaign-103", BASE).state is CampaignState.QUARANTINED


def test_chunk_digest_mismatch_rejected_without_state_change():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    with pytest.raises(IntegrityViolation):
        store.upload_evidence_chunk("campaign-103", "upload-1", 0, data[:10], "a" * 64, receipt, BASE)
    assert store.upload_manifest("upload-1").next_offset == 0


def test_chunks_must_be_contiguous():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    with pytest.raises(InvalidTransition):
        store.upload_evidence_chunk(
            "campaign-103", "upload-1", 1, data[:10], sha256_hex(data[:10]), receipt, BASE
        )


def test_chunk_cannot_exceed_configured_size():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    chunk = b"z" * 257
    with pytest.raises(ValueError):
        store.upload_evidence_chunk(
            "campaign-103", "upload-1", 0, chunk, sha256_hex(chunk), receipt, BASE
        )


def test_chunk_replay_is_idempotent_when_identical():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    chunk = data[:256]
    first = store.upload_evidence_chunk(
        "campaign-103", "upload-1", 0, chunk, sha256_hex(chunk), receipt, BASE
    )
    second = store.upload_evidence_chunk(
        "campaign-103", "upload-1", 0, chunk, sha256_hex(chunk), receipt, BASE
    )
    assert first.manifest_digest == second.manifest_digest


def test_chunk_replay_mismatch_quarantines():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    chunk = data[:256]
    store.upload_evidence_chunk(
        "campaign-103", "upload-1", 0, chunk, sha256_hex(chunk), receipt, BASE
    )
    changed = b"y" * 256
    with pytest.raises(IntegrityViolation):
        store.upload_evidence_chunk(
            "campaign-103", "upload-1", 0, changed, sha256_hex(changed), receipt, BASE
        )
    assert store.snapshot("campaign-103", BASE).state is CampaignState.QUARANTINED


def test_finalize_rejects_incomplete_upload():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    with pytest.raises(InvalidTransition):
        store.finalize_evidence_upload("campaign-103", "upload-1", receipt, BASE)


def test_finalize_success_releases_lease_and_schedules_next_run():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    upload_all(store, receipt, manifest, data)
    snapshot = store.finalize_evidence_upload("campaign-103", "upload-1", receipt, BASE)
    assert snapshot.state is CampaignState.READY
    assert snapshot.lease_owner is None
    assert snapshot.upload_state is UploadState.COMPLETE
    assert snapshot.evidence_digest == sha256_hex(data)
    assert snapshot.next_due_at == BASE + timedelta(hours=1)


def test_full_facade_run_read_only_probe():
    store = registered_store()
    receipt = lease(store)
    probe_plan = plan()
    evidence = verified_evidence(probe_plan)
    service = ReadOnlyCampaignControlPlaneV103(store)
    snapshot = service.run_read_only_probe(
        probe_plan,
        receipt,
        evidence,
        deterministic_evidence_bytes(evidence),
        "upload-facade",
        BASE,
    )
    assert snapshot.state is CampaignState.READY
    assert snapshot.external_order_routing_allowed is False
    assert snapshot.live_trading_allowed is False


def test_facade_rejects_plan_evidence_mismatch():
    store = registered_store()
    receipt = lease(store)
    probe_plan = plan()
    evidence = replace(verified_evidence(probe_plan), request_id="other")
    with pytest.raises(IntegrityViolation):
        ReadOnlyCampaignControlPlaneV103(store).run_read_only_probe(
            probe_plan, receipt, evidence, b"data", "upload-x", BASE
        )


def test_stale_heartbeat_blocks_campaign():
    store = registered_store()
    receipt = lease(store)
    heartbeat(store, receipt, BASE)
    blocked = store.recover_stale_workers(BASE + timedelta(minutes=3))
    assert blocked == ("campaign-103",)
    assert store.snapshot("campaign-103", BASE + timedelta(minutes=3)).state is CampaignState.BLOCKED


def test_expired_lease_blocks_campaign():
    store = registered_store()
    lease(store)
    blocked = store.recover_stale_workers(BASE + timedelta(minutes=6))
    assert blocked == ("campaign-103",)


def test_scheduler_tick_activates_and_recovers():
    store = InMemoryControlPlaneStoreV103()
    p = policy(starts_at=BASE + timedelta(minutes=1), ends_at=BASE + timedelta(days=1))
    store.register_campaign(p, BASE)
    service = ReadOnlyCampaignControlPlaneV103(store)
    assert service.scheduler_tick(BASE + timedelta(minutes=1))["activated"] == ("campaign-103",)


def test_incident_dedupes_and_escalates():
    store = registered_store()
    first = store.raise_incident("campaign-103", "NETWORK", IncidentSeverity.WARNING, {"n": 1}, BASE)
    second = store.raise_incident("campaign-103", "NETWORK", IncidentSeverity.CRITICAL, {"n": 2}, BASE)
    assert first.incident_id == second.incident_id
    assert second.severity is IncidentSeverity.CRITICAL


def test_incident_acknowledge_and_resolve():
    store = registered_store()
    incident = store.raise_incident("campaign-103", "NETWORK", IncidentSeverity.WARNING, {}, BASE)
    ack = store.acknowledge_incident(incident.incident_id, "operator", BASE)
    assert ack.status is IncidentStatus.ACKNOWLEDGED
    resolved = store.resolve_incident(incident.incident_id, "operator", BASE)
    assert resolved.status is IncidentStatus.RESOLVED


def test_incident_budget_creates_critical_budget_incident():
    store = InMemoryControlPlaneStoreV103()
    store.register_campaign(policy(maximum_open_incidents=1), BASE)
    store.raise_incident("campaign-103", "A", IncidentSeverity.WARNING, {}, BASE)
    incident = store.raise_incident("campaign-103", "B", IncidentSeverity.WARNING, {}, BASE)
    assert incident.code == "INCIDENT_BUDGET_EXHAUSTED"
    assert incident.severity is IncidentSeverity.CRITICAL


def test_critical_incident_blocks_lease():
    store = registered_store()
    store.raise_incident("campaign-103", "CRITICAL", IncidentSeverity.CRITICAL, {}, BASE)
    with pytest.raises(LeaseUnavailable):
        lease(store)


def test_operator_release_requires_resolved_critical_incident():
    store = registered_store()
    receipt = lease(store)
    store.recover_stale_workers(BASE + timedelta(minutes=6))
    with pytest.raises(InvalidTransition):
        store.operator_release_block("campaign-103", "operator", "checked", 1, BASE + timedelta(minutes=6))
    incident = store.incidents("campaign-103")[0]
    store.resolve_incident(incident.incident_id, "operator", BASE + timedelta(minutes=7))
    snapshot = store.operator_release_block("campaign-103", "operator", "checked", 1, BASE + timedelta(minutes=7))
    assert snapshot.state is CampaignState.READY
    assert snapshot.generation == 2


def test_old_generation_rejected_after_operator_release():
    store = registered_store()
    lease(store)
    store.recover_stale_workers(BASE + timedelta(minutes=6))
    incident = store.incidents("campaign-103")[0]
    store.resolve_incident(incident.incident_id, "operator", BASE + timedelta(minutes=7))
    store.operator_release_block("campaign-103", "operator", "checked", 1, BASE + timedelta(minutes=7))
    with pytest.raises(StaleGeneration):
        lease(store, BASE + timedelta(minutes=7), generation=1)


def test_retention_sweep_dry_run_and_delete():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    upload_all(store, receipt, manifest, data)
    store.finalize_evidence_upload("campaign-103", "upload-1", receipt, BASE)
    when = BASE + timedelta(days=8)
    assert store.retention_sweep(when, dry_run=True) == ("upload-1",)
    assert store.retention_sweep(when) == ("upload-1",)
    assert store.retention_sweep(when) == ()


def test_legal_hold_prevents_retention_delete():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    upload_all(store, receipt, manifest, data)
    store.finalize_evidence_upload("campaign-103", "upload-1", receipt, BASE)
    store.set_legal_hold("upload-1", True, "operator", BASE)
    assert store.retention_sweep(BASE + timedelta(days=8)) == ()


def test_retention_manifest_tamper_quarantines():
    store = registered_store()
    receipt, manifest, data = start_verified_upload(store)
    upload_all(store, receipt, manifest, data)
    store.finalize_evidence_upload("campaign-103", "upload-1", receipt, BASE)
    store.tamper_retained_manifest_for_test("upload-1")
    with pytest.raises(IntegrityViolation):
        store.retention_sweep(BASE + timedelta(days=8))
    assert store.snapshot("campaign-103", BASE + timedelta(days=8)).state is CampaignState.QUARANTINED


def test_readiness_is_fail_closed_for_memory_backend():
    store = registered_store()
    readiness = store.readiness("campaign-103", BASE)
    assert readiness["ready_for_read_only_probe"] is False
    assert "production_postgresql_backend_not_verified" in readiness["reasons"]
    assert readiness["external_order_routing_allowed"] is False
    assert readiness["live_trading_allowed"] is False


def test_event_chain_verifies_after_normal_flow():
    store = registered_store()
    receipt = lease(store)
    heartbeat(store, receipt)
    assert store.verify_event_chain("campaign-103") is True
    assert len(store.events("campaign-103")) >= 3


def test_concurrent_lease_only_one_winner():
    store = registered_store()
    winners = []
    errors = []

    def claim(owner):
        try:
            winners.append(lease(store, owner=owner))
        except LeaseUnavailable as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(errors) == 7


def test_snapshot_unknown_campaign_raises_key_error():
    store = InMemoryControlPlaneStoreV103()
    with pytest.raises(KeyError):
        store.snapshot("missing", BASE)


def test_idle_lease_can_be_released_gracefully():
    store = registered_store()
    receipt = lease(store)
    snapshot = store.release_lease("campaign-103", receipt, BASE)
    assert snapshot.state is CampaignState.READY
    assert snapshot.lease_owner is None
    assert store.events("campaign-103")[-1].event_type == "LEASE_RELEASED"


def test_lease_release_rejects_active_probe():
    store = registered_store()
    receipt = lease(store)
    store.begin_read_only_probe(plan(), receipt, BASE)
    with pytest.raises(InvalidTransition):
        store.release_lease("campaign-103", receipt, BASE)


def test_failed_probe_can_be_cleaned_then_released_by_operator():
    store = registered_store()
    receipt = lease(store)
    probe_plan = plan()
    store.begin_read_only_probe(probe_plan, receipt, BASE)
    failed = ReadOnlyProbeEvidenceV103(
        run_id=probe_plan.run_id,
        request_id=probe_plan.request_id,
        campaign_id=probe_plan.campaign_id,
        generation=probe_plan.generation,
        account_id=probe_plan.account_id,
        observed_at=BASE,
        outcome=ProbeOutcome.FAILED,
        account_read_verified=False,
        open_orders_read_verified=False,
        stream_auth_verified=False,
        mutation_count=0,
        external_order_routing_attempted=False,
        payload_digest="a" * 64,
        diagnostic_code="READ_FAILED",
    )
    store.record_probe_evidence(failed, receipt, BASE)
    cleanup = store.operator_confirm_cleanup(
        "campaign-103", "operator", "b" * 64, 0, "0.000", BASE + timedelta(seconds=1)
    )
    assert cleanup.active_run_id is None
    incident = store.incidents("campaign-103")[0]
    store.resolve_incident(incident.incident_id, "operator", BASE + timedelta(seconds=2))
    released = store.operator_release_block(
        "campaign-103", "operator", "broker state checked", 1, BASE + timedelta(seconds=3)
    )
    assert released.state is CampaignState.READY
    assert released.generation == 2


def test_operator_cleanup_rejects_residual_broker_state():
    store = registered_store()
    receipt = lease(store)
    store.recover_stale_workers(BASE + timedelta(minutes=6))
    with pytest.raises(InvalidTransition):
        store.operator_confirm_cleanup(
            "campaign-103", "operator", "b" * 64, 1, "0", BASE + timedelta(minutes=6)
        )
    with pytest.raises(InvalidTransition):
        store.operator_confirm_cleanup(
            "campaign-103", "operator", "b" * 64, 0, "0.01", BASE + timedelta(minutes=6)
        )
