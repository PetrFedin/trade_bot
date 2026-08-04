from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import threading

import pytest

from app.runtime.deployment_qualification_v106 import (
    BackupManifestV106,
    CertificateDrillStateV106,
    CertificateRenewalDrillV106,
    CertificateSnapshotV106,
    DependencySnapshotV106,
    DeploymentPolicyV106,
    DeploymentQualificationCoordinatorV106,
    DisasterRecoveryDrillV106,
    DisasterRecoveryStateV106,
    DisruptionBudgetSnapshotV106,
    GateSeverityV106,
    KubernetesDeploymentSnapshotV106,
    ManifestReplayLedgerV106,
    NetworkPolicySnapshotV106,
    ObservationSampleV106,
    PodSnapshotV106,
    QualificationEvidenceBundleV106,
    QualificationJournalV106,
    QualificationStateV106,
    ReplayErrorV106,
    RestoreEvidenceV106,
    RolloutActionStatusV106,
    RolloutActionTypeV106,
    SignatureErrorV106,
    SignedDeploymentManifestV106,
    StateTransitionErrorV106,
    ValidationErrorV106,
    evaluate_observation_v106,
    evaluate_preflight_v106,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
SECRET = b"schema106-manifest-secret-material-0001"
ACTION_SECRET = b"schema106-rollout-action-secret-0001"
IMAGE = "sha256:" + "a" * 64
CONFIG = "sha256:" + "b" * 64
HEX_C = "c" * 64
HEX_D = "d" * 64


def policy(**changes):
    base = DeploymentPolicyV106(
        fleet_id="fleet-1",
        environment="paper-prod",
        expected_cluster="cluster-1",
        namespace="astra",
        service_account="astra-worker",
        expected_image_digest=IMAGE,
        expected_config_digest=CONFIG,
        allowed_kubernetes_hosts=("kube.internal",),
        allowed_s3_hosts=("evidence.internal",),
        min_replicas=2,
        max_replicas=6,
        canary_replicas=1,
        min_ready_replicas=2,
        min_observation_samples=3,
        min_observation_seconds=120,
        required_zones=("zone-a", "zone-b"),
    )
    return replace(base, **changes)


def manifest(p=None, **changes):
    p = p or policy()
    values = dict(
        manifest_id="manifest-1",
        rollout_id="rollout-1",
        deployment_id="deploy-1",
        fleet_id=p.fleet_id,
        environment=p.environment,
        generation=7,
        image_digest=p.expected_image_digest,
        config_digest=p.expected_config_digest,
        replicas=3,
        canary_replicas=p.canary_replicas,
        issued_at=NOW - timedelta(seconds=10),
        not_before=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(hours=1),
        nonce="nonce-1",
        key_id="key-1",
        secret=SECRET,
    )
    values.update(changes)
    return SignedDeploymentManifestV106.sign(**values)


def pod(worker: str, zone: str, *, canary=False, **changes):
    values = dict(
        pod_uid=f"pod-{worker}",
        worker_id=worker,
        zone=zone,
        image_digest=IMAGE,
        config_digest=CONFIG,
        ready=True,
        is_canary=canary,
        restart_count=0,
        heartbeat_at=NOW - timedelta(seconds=5),
        certificate_not_after=NOW + timedelta(hours=2),
        active_claims=0,
        evidence_pending=0,
        broker_mutation_count=0,
    )
    values.update(changes)
    return PodSnapshotV106(**values)


def network(p=None, **changes):
    p = p or policy()
    required = set(p.required_egress) | {f"{host}:443/tcp" for host in p.allowed_s3_hosts}
    values = dict(default_deny_ingress=True, default_deny_egress=True, allowed_egress=tuple(sorted(required)))
    values.update(changes)
    return NetworkPolicySnapshotV106(**values)


def snapshot(p=None, **changes):
    p = p or policy()
    pods = (pod("worker-1", "zone-a", canary=True), pod("worker-2", "zone-b"))
    values = dict(
        cluster=p.expected_cluster,
        namespace=p.namespace,
        service_account=p.service_account,
        deployment_id="deploy-1",
        generation=7,
        observed_at=NOW,
        desired_replicas=2,
        available_replicas=2,
        canary_ready_replicas=1,
        zone_replicas=(("zone-a", 1), ("zone-b", 1)),
        pods=pods,
        network_policy=network(p),
        disruption_budget=DisruptionBudgetSnapshotV106(min_available=2, max_unavailable=None, unhealthy_pod_eviction_policy="IfHealthyBudget"),
    )
    values.update(changes)
    return KubernetesDeploymentSnapshotV106(**values)


def dependencies(**changes):
    values = dict(
        observed_at=NOW,
        postgres_ready=True,
        object_storage_ready=True,
        control_plane_ready=True,
        identity_authority_ready=True,
        clock_offset_seconds=0.2,
        backup_age_seconds=60,
        postgres_evidence_digest=HEX_C,
        object_storage_evidence_digest=HEX_D,
    )
    values.update(changes)
    return DependencySnapshotV106(**values)


def observation(index: int, *, seconds: int | None = None, **changes):
    if seconds is None:
        seconds = index * 60
    values = dict(
        sample_id=f"sample-{index}",
        observed_at=NOW + timedelta(seconds=seconds),
        ready_replicas=2,
        canary_ready_replicas=1,
        request_count=1000,
        error_count=0,
        p95_latency_ms=100,
        stale_heartbeats=0,
        crashloops=0,
        dlq_depth=0,
        open_incidents=0,
        broker_mutation_count=0,
        external_order_routing_allowed=False,
        live_trading_allowed=False,
        evidence_digest=hashlib.sha256(f"sample-{index}".encode()).hexdigest(),
    )
    values.update(changes)
    return ObservationSampleV106(**values)


def started_coordinator(p=None):
    p = p or policy()
    m = manifest(p)
    coordinator = DeploymentQualificationCoordinatorV106("qualification-1", p, m, {"key-1": SECRET, "action-key": ACTION_SECRET})
    gates = coordinator.start(snapshot=snapshot(p), dependencies=dependencies(), now=NOW, replay_ledger=ManifestReplayLedgerV106())
    assert gates.passed
    return coordinator


def test_policy_identity_and_safety_boundaries():
    p = policy()
    assert len(p.policy_digest) == 64
    assert p.external_order_routing_allowed is False
    assert p.live_trading_allowed is False


@pytest.mark.parametrize(
    "changes",
    [
        {"fleet_id": "bad id"},
        {"expected_image_digest": "bad"},
        {"allowed_kubernetes_hosts": ()},
        {"paper_broker_host": "api.alpaca.markets"},
        {"min_replicas": 0},
        {"max_replicas": 1},
        {"canary_replicas": 3},
        {"min_ready_replicas": 99},
        {"max_unavailable": -1},
        {"max_error_rate_bps": 10001},
        {"min_observation_samples": 0},
        {"max_restart_count": -1},
        {"required_zones": ()},
        {"required_zones": ("zone-a", "zone-a")},
        {"required_egress": ("0.0.0.0/0:443/tcp",)},
        {"external_order_routing_allowed": True},
        {"live_trading_allowed": True},
    ],
)
def test_policy_rejects_invalid_configuration(changes):
    with pytest.raises(ValidationErrorV106):
        policy(**changes)


def test_manifest_sign_verify_and_replay():
    p = policy()
    m = manifest(p)
    ledger = ManifestReplayLedgerV106()
    m.verify(policy=p, keyring={"key-1": SECRET}, now=NOW, replay_ledger=ledger)
    assert len(ledger) == 1
    with pytest.raises(ReplayErrorV106):
        m.verify(policy=p, keyring={"key-1": SECRET}, now=NOW, replay_ledger=ledger)


@pytest.mark.parametrize(
    "mutator,error",
    [
        (lambda m: replace(m, signature="0" * 64), SignatureErrorV106),
        (lambda m: replace(m, key_id="missing"), SignatureErrorV106),
        (lambda m: replace(m, fleet_id="other"), SignatureErrorV106),
    ],
)
def test_manifest_rejects_bad_signature_scope_changes(mutator, error):
    p = policy()
    m = mutator(manifest(p))
    with pytest.raises(error):
        m.verify(policy=p, keyring={"key-1": SECRET}, now=NOW)


def test_manifest_rejects_time_and_release_mismatch():
    p = policy()
    expired = manifest(p, expires_at=NOW - timedelta(seconds=1), not_before=NOW - timedelta(seconds=2), issued_at=NOW - timedelta(seconds=3))
    with pytest.raises(SignatureErrorV106):
        expired.verify(policy=p, keyring={"key-1": SECRET}, now=NOW)
    different = replace(p, expected_config_digest="sha256:" + "e" * 64)
    with pytest.raises(ValidationErrorV106):
        manifest(p).verify(policy=different, keyring={"key-1": SECRET}, now=NOW)


def test_replay_ledger_is_thread_safe():
    ledger = ManifestReplayLedgerV106()
    failures = []
    barrier = threading.Barrier(8)

    def consume():
        barrier.wait()
        try:
            ledger.consume("manifest-1", "nonce-1", NOW)
        except ReplayErrorV106:
            failures.append(1)

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ledger) == 1
    assert len(failures) == 7


def test_preflight_passes_complete_read_only_snapshot():
    p = policy()
    gates = evaluate_preflight_v106(policy=p, manifest=manifest(p), snapshot=snapshot(p), dependencies=dependencies(), now=NOW)
    assert gates.passed
    assert not gates.critical_failures
    assert len(gates.gates) >= 20


@pytest.mark.parametrize(
    "snapshot_change,dependency_change,failed_gate",
    [
        ({"cluster": "other"}, {}, "scope"),
        ({"generation": 8}, {}, "generation"),
        ({"desired_replicas": 0, "available_replicas": 0}, {}, "replica_window"),
        ({"canary_ready_replicas": 0}, {}, "canary_ready"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, image_digest="sha256:" + "f" * 64), pod("worker-2", "zone-b"))}, {}, "release_identity"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, restart_count=9), pod("worker-2", "zone-b"))}, {}, "restart_budget"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, heartbeat_at=NOW - timedelta(hours=1)), pod("worker-2", "zone-b"))}, {}, "heartbeat_freshness"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, certificate_not_after=NOW + timedelta(minutes=1)), pod("worker-2", "zone-b"))}, {}, "certificate_lifetime"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, active_claims=1), pod("worker-2", "zone-b"))}, {}, "claim_isolation"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, evidence_pending=1), pod("worker-2", "zone-b"))}, {}, "evidence_spool"),
        ({"pods": (pod("worker-1", "zone-a", canary=True, broker_mutation_count=1), pod("worker-2", "zone-b"))}, {}, "broker_mutations"),
        ({"external_order_routing_allowed": True}, {}, "routing_boundary"),
        ({"network_policy": network(default_deny_egress=False)}, {}, "network_default_deny"),
        ({"network_policy": network(allowed_egress=("dns:53/udp",))}, {}, "network_allowlist"),
        ({"network_policy": network(broad_cidrs=("0.0.0.0/0",))}, {}, "network_no_broad_cidr"),
        ({"network_policy": network(live_hosts=("api.alpaca.markets",))}, {}, "network_no_live_host"),
        ({"disruption_budget": DisruptionBudgetSnapshotV106(min_available=0, max_unavailable=5, unhealthy_pod_eviction_policy="IfHealthyBudget")}, {}, "disruption_budget"),
        ({"zone_replicas": (("zone-a", 2),)}, {}, "zone_spread"),
        ({}, {"postgres_ready": False}, "postgres_ready"),
        ({}, {"object_storage_ready": False}, "object_storage_ready"),
        ({}, {"control_plane_ready": False}, "control_plane_ready"),
        ({}, {"identity_authority_ready": False}, "identity_authority_ready"),
        ({}, {"clock_offset_seconds": 99.0}, "clock_skew"),
        ({}, {"backup_age_seconds": 999999}, "backup_freshness"),
    ],
)
def test_preflight_blocks_each_critical_boundary(snapshot_change, dependency_change, failed_gate):
    p = policy()
    snap = snapshot(p, **snapshot_change)
    deps = dependencies(**dependency_change)
    gates = evaluate_preflight_v106(policy=p, manifest=manifest(p), snapshot=snap, dependencies=deps, now=NOW)
    assert not gates.passed
    assert failed_gate in {gate.name for gate in gates.critical_failures}


def test_observation_error_rate_and_warning():
    p = policy()
    sample = observation(1, request_count=0, error_count=0)
    gates = evaluate_observation_v106(p, sample)
    assert gates.passed
    traffic = next(gate for gate in gates.gates if gate.name == "traffic_observed")
    assert traffic.severity == GateSeverityV106.WARNING
    assert not traffic.passed


@pytest.mark.parametrize(
    "changes,gate",
    [
        ({"ready_replicas": 0}, "ready_replicas"),
        ({"canary_ready_replicas": 0}, "canary_ready"),
        ({"request_count": 100, "error_count": 1}, "error_rate"),
        ({"p95_latency_ms": 9999}, "latency"),
        ({"stale_heartbeats": 1}, "heartbeats"),
        ({"crashloops": 1}, "crashloops"),
        ({"dlq_depth": 1}, "dlq"),
        ({"open_incidents": 1}, "incidents"),
        ({"broker_mutation_count": 1}, "broker_mutations"),
        ({"live_trading_allowed": True}, "routing_boundary"),
    ],
)
def test_observation_gate_failures(changes, gate):
    gates = evaluate_observation_v106(policy(), observation(1, **changes))
    assert not gates.passed
    assert gate in {item.name for item in gates.critical_failures}


def test_coordinator_full_promotion_and_completion_flow():
    coordinator = started_coordinator()
    assert coordinator.state == QualificationStateV106.CANARY
    for index in range(3):
        coordinator.record_observation(observation(index + 1, seconds=index * 60))
    assert coordinator.assess_promotable(NOW + timedelta(seconds=120))
    assert coordinator.state == QualificationStateV106.PROMOTABLE
    action = coordinator.create_action(
        action_id="action-1",
        action=RolloutActionTypeV106.PROMOTE,
        approver_a="operator-a",
        approver_b="operator-b",
        key_id="action-key",
        secret=ACTION_SECRET,
        now=NOW + timedelta(seconds=121),
        reason_digest=HEX_C,
    )
    assert action.status == RolloutActionStatusV106.PENDING
    assert coordinator.claim_action().attempt_count == 1
    coordinator.acknowledge_action(success=True, receipt_digest=HEX_D, observed_at=NOW + timedelta(seconds=122))
    assert coordinator.state == QualificationStateV106.PROMOTED
    full = snapshot(observed_at=NOW + timedelta(seconds=123), desired_replicas=3, available_replicas=3, pods=(pod("worker-1", "zone-a", canary=True, heartbeat_at=NOW + timedelta(seconds=120)), pod("worker-2", "zone-b", heartbeat_at=NOW + timedelta(seconds=120)), pod("worker-3", "zone-a", heartbeat_at=NOW + timedelta(seconds=120))), zone_replicas=(("zone-a", 2), ("zone-b", 1)))
    result = coordinator.complete_rollout(snapshot=full, dependencies=dependencies(observed_at=NOW + timedelta(seconds=123)), now=NOW + timedelta(seconds=123))
    assert result.passed
    assert coordinator.state == QualificationStateV106.COMPLETED
    assert coordinator.journal.verify()
    bundle = QualificationEvidenceBundleV106.from_coordinator(coordinator)
    assert bundle.final_state == QualificationStateV106.COMPLETED
    assert len(bundle.bundle_digest) == 64


def test_coordinator_blocks_bad_preflight():
    p = policy()
    coordinator = DeploymentQualificationCoordinatorV106("qualification-1", p, manifest(p), {"key-1": SECRET})
    result = coordinator.start(snapshot=snapshot(p, live_trading_allowed=True), dependencies=dependencies(), now=NOW, replay_ledger=ManifestReplayLedgerV106())
    assert not result.passed
    assert coordinator.state == QualificationStateV106.BLOCKED


def test_coordinator_quarantines_broker_mutation():
    coordinator = started_coordinator()
    coordinator.record_observation(observation(1, broker_mutation_count=1))
    assert coordinator.state == QualificationStateV106.QUARANTINED


def test_coordinator_blocks_non_boundary_failure_budget():
    coordinator = started_coordinator(policy(max_failure_samples=0))
    coordinator.record_observation(observation(1, seconds=0, p95_latency_ms=9999))
    assert coordinator.state == QualificationStateV106.BLOCKED


def test_observation_replay_and_time_regression():
    coordinator = started_coordinator()
    first = observation(1, seconds=60)
    coordinator.record_observation(first)
    with pytest.raises(ReplayErrorV106):
        coordinator.record_observation(first)
    with pytest.raises(ValidationErrorV106):
        coordinator.record_observation(observation(2, seconds=30))


def test_promotion_requires_sample_count_window_and_freshness():
    coordinator = started_coordinator()
    coordinator.record_observation(observation(1, seconds=0))
    coordinator.record_observation(observation(2, seconds=60))
    assert not coordinator.assess_promotable(NOW + timedelta(seconds=60))
    coordinator.record_observation(observation(3, seconds=120))
    assert not coordinator.assess_promotable(NOW + timedelta(hours=1))


def test_action_requires_state_dual_control_and_single_attempt():
    coordinator = started_coordinator()
    with pytest.raises(StateTransitionErrorV106):
        coordinator.create_action(action_id="a", action=RolloutActionTypeV106.PROMOTE, approver_a="x", approver_b="y", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)
    for index in range(3):
        coordinator.record_observation(observation(index + 1, seconds=index * 60))
    coordinator.assess_promotable(NOW + timedelta(seconds=120))
    with pytest.raises(ValidationErrorV106):
        coordinator.create_action(action_id="a", action=RolloutActionTypeV106.PROMOTE, approver_a="x", approver_b="x", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)
    action = coordinator.create_action(action_id="a", action=RolloutActionTypeV106.PROMOTE, approver_a="x", approver_b="y", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)
    action.verify({"action-key": ACTION_SECRET})
    claimed = coordinator.claim_action()
    with pytest.raises(StateTransitionErrorV106):
        claimed.claim()
    failed = coordinator.acknowledge_action(success=False, receipt_digest=HEX_D, observed_at=NOW)
    assert failed.status == RolloutActionStatusV106.FAILED
    assert coordinator.state == QualificationStateV106.QUARANTINED


def test_rollback_flow_from_canary():
    coordinator = started_coordinator()
    coordinator.create_action(action_id="rollback-1", action=RolloutActionTypeV106.ROLLBACK, approver_a="a", approver_b="b", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)
    coordinator.claim_action()
    coordinator.acknowledge_action(success=True, receipt_digest=HEX_D, observed_at=NOW)
    assert coordinator.state == QualificationStateV106.ROLLED_BACK


def test_completion_quarantines_incomplete_full_rollout():
    coordinator = started_coordinator()
    for index in range(3):
        coordinator.record_observation(observation(index + 1, seconds=index * 60))
    coordinator.assess_promotable(NOW + timedelta(seconds=120))
    coordinator.create_action(action_id="action-1", action=RolloutActionTypeV106.PROMOTE, approver_a="a", approver_b="b", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)
    coordinator.claim_action()
    coordinator.acknowledge_action(success=True, receipt_digest=HEX_D, observed_at=NOW)
    result = coordinator.complete_rollout(snapshot=snapshot(), dependencies=dependencies(), now=NOW)
    assert not result.passed
    assert coordinator.state == QualificationStateV106.QUARANTINED


def test_journal_detects_tampering():
    journal = QualificationJournalV106()
    first = journal.append("ONE", {"value": 1}, NOW)
    journal.append("TWO", {"value": 2}, NOW + timedelta(seconds=1))
    assert journal.verify()
    journal._events[0] = replace(first, payload_digest="f" * 64)  # deliberate corruption for qualification test
    assert not journal.verify()


def certificate(worker="worker-1", generation=1, fingerprint="1" * 64, *, not_before=None, not_after=None, heartbeat=None, active_claims=0):
    return CertificateSnapshotV106(
        worker_id=worker,
        identity_generation=generation,
        fingerprint=fingerprint,
        serial_digest="2" * 64,
        issuer_digest="3" * 64,
        not_before=not_before or (NOW - timedelta(minutes=10)),
        not_after=not_after or (NOW + timedelta(minutes=10)),
        heartbeat_at=heartbeat or NOW,
        active_claims=active_claims,
    )


def test_certificate_renewal_drill_full_flow():
    p = policy(cert_min_remaining_seconds=300, cert_max_overlap_seconds=900)
    old = certificate(not_after=NOW + timedelta(minutes=10))
    new = certificate(generation=2, fingerprint="4" * 64, not_before=NOW, not_after=NOW + timedelta(hours=2))
    drill = CertificateRenewalDrillV106("cert-drill-1", p, old, "operator-a", "operator-b")
    drill.issue(new, NOW)
    drill.activate(NOW + timedelta(seconds=1))
    drill.revoke_old(NOW + timedelta(seconds=2))
    assert drill.verify(new, NOW + timedelta(seconds=3))
    assert drill.state == CertificateDrillStateV106.VERIFIED
    assert drill.journal.verify()


@pytest.mark.parametrize(
    "new_certificate,error_stage",
    [
        (certificate(worker="worker-2", generation=2, fingerprint="4" * 64), "issue"),
        (certificate(generation=3, fingerprint="4" * 64), "issue"),
        (certificate(generation=2, fingerprint="1" * 64), "issue"),
        (certificate(generation=2, fingerprint="4" * 64, active_claims=1), "activate"),
        (certificate(generation=2, fingerprint="4" * 64, not_before=NOW - timedelta(hours=1), not_after=NOW + timedelta(hours=2)), "activate"),
    ],
)
def test_certificate_drill_rejects_invalid_transitions(new_certificate, error_stage):
    p = policy(cert_max_overlap_seconds=900)
    old = certificate(not_after=NOW + timedelta(minutes=10))
    drill = CertificateRenewalDrillV106("cert-drill-1", p, old, "a", "b")
    if error_stage == "issue":
        with pytest.raises(ValidationErrorV106):
            drill.issue(new_certificate, NOW)
    else:
        drill.issue(new_certificate, NOW)
        with pytest.raises(ValidationErrorV106):
            drill.activate(NOW)


def test_certificate_verification_failure():
    p = policy(cert_min_remaining_seconds=300)
    old = certificate(not_after=NOW + timedelta(minutes=10))
    new = certificate(generation=2, fingerprint="4" * 64, not_before=NOW, not_after=NOW + timedelta(hours=2))
    drill = CertificateRenewalDrillV106("cert-drill-1", p, old, "a", "b")
    drill.issue(new, NOW)
    drill.activate(NOW)
    drill.revoke_old(NOW)
    bad = replace(new, heartbeat_at=NOW - timedelta(hours=1))
    assert not drill.verify(bad, NOW)
    assert drill.state == CertificateDrillStateV106.FAILED


def backup(**changes):
    values = dict(
        backup_id="backup-1",
        source_environment="paper-prod",
        created_at=NOW - timedelta(minutes=6),
        completed_at=NOW - timedelta(minutes=5),
        object_digest="5" * 64,
        size_bytes=1000,
        postgres_lsn="lsn-1",
        schema_version="v106",
        encrypted=True,
        kms_key_id="kms-1",
        integrity_digest="6" * 64,
    )
    values.update(changes)
    return BackupManifestV106(**values)


def restore(**changes):
    values = dict(
        target_environment="drill-20260804",
        started_at=NOW - timedelta(minutes=4),
        completed_at=NOW - timedelta(minutes=2),
        restored_lsn="lsn-1",
        schema_version="v106",
        integrity_digest="6" * 64,
        postgres_ready=True,
        object_storage_ready=True,
        external_order_routing_allowed=False,
        live_trading_allowed=False,
    )
    values.update(changes)
    return RestoreEvidenceV106(**values)


def test_disaster_recovery_drill_passes_isolated_restore():
    drill = DisasterRecoveryDrillV106("drill-1", policy(), backup())
    drill.start(NOW)
    gates = drill.verify(restore(), NOW)
    assert gates.passed
    assert drill.state == DisasterRecoveryStateV106.PASSED
    assert drill.journal.verify()


@pytest.mark.parametrize(
    "changes,gate",
    [
        ({"target_environment": "paper-prod"}, "isolated_target"),
        ({"started_at": NOW + timedelta(hours=1), "completed_at": NOW + timedelta(hours=1, minutes=1)}, "rpo"),
        ({"started_at": NOW - timedelta(hours=1), "completed_at": NOW}, "rto"),
        ({"restored_lsn": "lsn-2"}, "postgres_lsn"),
        ({"schema_version": "v105"}, "schema_version"),
        ({"integrity_digest": "7" * 64}, "integrity"),
        ({"postgres_ready": False}, "dependencies"),
        ({"live_trading_allowed": True}, "routing_boundary"),
    ],
)
def test_disaster_recovery_gate_failures(changes, gate):
    drill = DisasterRecoveryDrillV106("drill-1", policy(), backup())
    drill.start(NOW)
    gates = drill.verify(restore(**changes), NOW)
    assert not gates.passed
    assert gate in {item.name for item in gates.critical_failures}
    assert drill.state == DisasterRecoveryStateV106.QUARANTINED


def test_disaster_recovery_rejects_stale_or_wrong_source_backup():
    with pytest.raises(ValidationErrorV106):
        DisasterRecoveryDrillV106("drill-1", policy(), backup(source_environment="other")).start(NOW)
    with pytest.raises(ValidationErrorV106):
        DisasterRecoveryDrillV106("drill-1", policy(backup_max_age_seconds=60), backup(completed_at=NOW - timedelta(hours=1), created_at=NOW - timedelta(hours=2))).start(NOW)


def test_dataclass_validations_cover_invalid_inputs():
    with pytest.raises(ValidationErrorV106):
        ObservationSampleV106("bad id", NOW, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, False, False, HEX_C)
    with pytest.raises(ValidationErrorV106):
        NetworkPolicySnapshotV106(True, True, ("a", "a"))
    with pytest.raises(ValidationErrorV106):
        DisruptionBudgetSnapshotV106(None, None, "IfHealthyBudget")
    with pytest.raises(ValidationErrorV106):
        KubernetesDeploymentSnapshotV106("c", "n", "s", "d", 1, NOW, 1, 2, 0, (), (), network(), DisruptionBudgetSnapshotV106(1, None, "IfHealthyBudget"))
    with pytest.raises(ValidationErrorV106):
        BackupManifestV106("b", "e", NOW, NOW, HEX_C, 0, "l", "v", True, "k", HEX_D)
    with pytest.raises(ValidationErrorV106):
        RestoreEvidenceV106("drill-x", NOW, NOW, "l", "v", HEX_C, True, True, False, False)


def test_private_canonical_and_validator_error_paths():
    from app.runtime.deployment_qualification_v106 import _canonical, _ensure_utc

    assert b'"CRITICAL"' == _canonical(GateSeverityV106.CRITICAL)
    assert b'manifest_id' in _canonical(manifest())
    assert b'pod_uid' in _canonical(pod("worker-x", "zone-a"))
    with pytest.raises(TypeError):
        _canonical(object())
    with pytest.raises(ValidationErrorV106):
        _ensure_utc(datetime(2026, 1, 1))
    with pytest.raises(ValidationErrorV106):
        NetworkPolicySnapshotV106(True, True, (), live_hosts=("bad host",))


def test_more_policy_validation_branches():
    with pytest.raises(ValidationErrorV106):
        policy(allowed_s3_hosts=("api.alpaca.markets",))
    with pytest.raises(ValidationErrorV106):
        policy(required_egress=())
    with pytest.raises(ValidationErrorV106):
        policy(required_egress=("dns:53/udp", "dns:53/udp"))
    with pytest.raises(ValidationErrorV106):
        policy(required_zones=("bad zone",))
    with pytest.raises(ValidationErrorV106):
        policy(allowed_kubernetes_hosts=("bad..host",))


def test_manifest_validation_and_policy_mismatch_branches():
    p = policy()
    values = dict(
        manifest_id="m",
        rollout_id="r",
        deployment_id="d",
        fleet_id=p.fleet_id,
        environment=p.environment,
        generation=1,
        image_digest=IMAGE,
        config_digest=CONFIG,
        replicas=2,
        canary_replicas=1,
        issued_at=NOW,
        not_before=NOW,
        expires_at=NOW + timedelta(minutes=1),
        nonce="n",
        key_id="k",
        secret=SECRET,
    )
    with pytest.raises(ValidationErrorV106):
        SignedDeploymentManifestV106.sign(**(values | {"generation": 0}))
    with pytest.raises(ValidationErrorV106):
        SignedDeploymentManifestV106.sign(**(values | {"issued_at": NOW + timedelta(seconds=2), "not_before": NOW + timedelta(seconds=1)}))
    with pytest.raises(ValidationErrorV106):
        SignedDeploymentManifestV106.sign(**(values | {"secret": b"weak"}))

    scope = SignedDeploymentManifestV106.sign(**(values | {"fleet_id": "other"}))
    with pytest.raises(ValidationErrorV106, match="scope"):
        scope.verify(policy=p, keyring={"k": SECRET}, now=NOW)
    replicas = SignedDeploymentManifestV106.sign(**(values | {"replicas": 1}))
    with pytest.raises(ValidationErrorV106, match="replicas"):
        replicas.verify(policy=p, keyring={"k": SECRET}, now=NOW)
    canary = SignedDeploymentManifestV106.sign(**(values | {"canary_replicas": 2}))
    with pytest.raises(ValidationErrorV106, match="canary"):
        canary.verify(policy=p, keyring={"k": SECRET}, now=NOW)
    with pytest.raises(SignatureErrorV106):
        SignedDeploymentManifestV106.sign(**values).verify(policy=p, keyring={"k": b"weak"}, now=NOW)


def test_replay_ledger_nonce_collision_with_different_manifest():
    ledger = ManifestReplayLedgerV106()
    ledger.consume("manifest-a", "nonce-a", NOW)
    with pytest.raises(ReplayErrorV106, match="nonce"):
        ledger.consume("manifest-b", "nonce-a", NOW)


def test_snapshot_and_gate_validation_error_branches():
    assert len(pod("worker-x", "zone-a").digest) == 64
    assert len(network().digest) == 64
    assert len(DisruptionBudgetSnapshotV106(1, None, "IfHealthyBudget").digest) == 64
    assert len(snapshot().digest) == 64
    assert len(dependencies().digest) == 64

    with pytest.raises(ValidationErrorV106):
        pod("worker-x", "zone-a", active_claims=-1)
    with pytest.raises(ValidationErrorV106):
        DisruptionBudgetSnapshotV106(-1, None, "IfHealthyBudget")
    with pytest.raises(ValidationErrorV106):
        DisruptionBudgetSnapshotV106(None, -1, "IfHealthyBudget")
    with pytest.raises(ValidationErrorV106):
        DisruptionBudgetSnapshotV106(1, None, "BadPolicy")
    with pytest.raises(ValidationErrorV106):
        snapshot(generation=0)
    with pytest.raises(ValidationErrorV106):
        snapshot(desired_replicas=-1, available_replicas=0)
    duplicate = pod("worker-1", "zone-a", canary=True)
    with pytest.raises(ValidationErrorV106, match="pod uid"):
        snapshot(pods=(duplicate, replace(duplicate, worker_id="worker-2")))
    with pytest.raises(ValidationErrorV106, match="worker id"):
        snapshot(pods=(duplicate, replace(duplicate, pod_uid="pod-other")))
    with pytest.raises(ValidationErrorV106, match="zone replica"):
        snapshot(zone_replicas=(("zone-a", 1), ("zone-a", 1)))
    with pytest.raises(ValidationErrorV106):
        dependencies(backup_age_seconds=-1)

    from app.runtime.deployment_qualification_v106 import GateEvaluationV106, GateSetV106
    with pytest.raises(ValidationErrorV106):
        GateEvaluationV106("gate", True, GateSeverityV106.CRITICAL, "", HEX_C)
    with pytest.raises(ValidationErrorV106):
        GateEvaluationV106("gate", True, GateSeverityV106.CRITICAL, "ok", "bad")
    with pytest.raises(ValidationErrorV106):
        GateSetV106(())
    gate = GateEvaluationV106("gate", True, GateSeverityV106.CRITICAL, "ok", HEX_C)
    assert len(gate.digest) == 64
    with pytest.raises(ValidationErrorV106):
        GateSetV106((gate, gate))


def test_observation_and_journal_validation_branches():
    with pytest.raises(ValidationErrorV106):
        observation(1, ready_replicas=-1)
    with pytest.raises(ValidationErrorV106):
        observation(1, request_count=1, error_count=2)
    from app.runtime.deployment_qualification_v106 import JournalEventV106
    with pytest.raises(ValidationErrorV106):
        JournalEventV106(0, "EVENT", NOW, HEX_C, HEX_D, "e" * 64)
    journal = QualificationJournalV106()
    assert journal.tail_digest == "0" * 64
    assert journal.snapshot() == ()
    first = journal.append("ONE", {"x": 1}, NOW)
    second = journal.append("TWO", {"x": 2}, NOW + timedelta(seconds=1))
    journal._events[1] = replace(second, sequence=3)
    assert not journal.verify()


def test_rollout_action_validation_and_signature_errors():
    from app.runtime.deployment_qualification_v106 import RolloutActionV106
    with pytest.raises(ValidationErrorV106):
        RolloutActionV106.sign(action_id="a", qualification_id="q", action=RolloutActionTypeV106.PROMOTE, created_at=NOW, approver_a="x", approver_b="y", evidence_digest=HEX_C, state_digest=HEX_D, idempotency_key="i", key_id="k", secret=b"weak")
    action = RolloutActionV106.sign(action_id="a", qualification_id="q", action=RolloutActionTypeV106.PROMOTE, created_at=NOW, approver_a="x", approver_b="y", evidence_digest=HEX_C, state_digest=HEX_D, idempotency_key="i", key_id="k", secret=ACTION_SECRET)
    with pytest.raises(SignatureErrorV106):
        action.verify({})
    with pytest.raises(SignatureErrorV106):
        replace(action, signature="0" * 64).verify({"k": ACTION_SECRET})
    with pytest.raises(ValidationErrorV106):
        replace(action, attempt_count=2)
    with pytest.raises(StateTransitionErrorV106):
        action.acknowledge(success=True, receipt_digest=HEX_C)
    with pytest.raises(ValidationErrorV106):
        action.claim().acknowledge(success=True, receipt_digest="bad")


def test_coordinator_remaining_state_branches():
    p = policy(max_failure_samples=1)
    coordinator = started_coordinator(p)
    with pytest.raises(StateTransitionErrorV106):
        coordinator.start(snapshot=snapshot(p), dependencies=dependencies(), now=NOW, replay_ledger=ManifestReplayLedgerV106())
    coordinator.record_observation(observation(1, seconds=0, p95_latency_ms=9999))
    assert coordinator.state == QualificationStateV106.OBSERVING
    coordinator.record_observation(observation(2, seconds=60))
    coordinator.record_observation(observation(3, seconds=120))
    assert not coordinator.assess_promotable(NOW + timedelta(seconds=120))
    coordinator.failure_samples = 2
    assert not coordinator.assess_promotable(NOW + timedelta(seconds=120))

    fresh = started_coordinator()
    assert not fresh.assess_promotable(NOW)
    with pytest.raises(StateTransitionErrorV106):
        fresh.claim_action()
    with pytest.raises(StateTransitionErrorV106):
        fresh.acknowledge_action(success=True, receipt_digest=HEX_C, observed_at=NOW)
    with pytest.raises(StateTransitionErrorV106):
        fresh.complete_rollout(snapshot=snapshot(), dependencies=dependencies(), now=NOW)
    fresh.state = QualificationStateV106.COMPLETED
    with pytest.raises(StateTransitionErrorV106):
        fresh.record_observation(observation(1))
    with pytest.raises(StateTransitionErrorV106):
        fresh.create_action(action_id="r", action=RolloutActionTypeV106.ROLLBACK, approver_a="a", approver_b="b", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)


def test_coordinator_rejects_second_action():
    coordinator = started_coordinator()
    coordinator.create_action(action_id="r", action=RolloutActionTypeV106.ROLLBACK, approver_a="a", approver_b="b", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)
    with pytest.raises(StateTransitionErrorV106):
        coordinator.create_action(action_id="r2", action=RolloutActionTypeV106.ROLLBACK, approver_a="a", approver_b="b", key_id="action-key", secret=ACTION_SECRET, now=NOW, reason_digest=HEX_C)


def test_certificate_remaining_validation_and_transition_branches():
    with pytest.raises(ValidationErrorV106):
        certificate(generation=0)
    with pytest.raises(ValidationErrorV106):
        certificate(not_before=NOW, not_after=NOW)
    drill = CertificateRenewalDrillV106("d", policy(), certificate(), "a", "b")
    with pytest.raises(StateTransitionErrorV106):
        drill.activate(NOW)
    with pytest.raises(StateTransitionErrorV106):
        drill.revoke_old(NOW)
    with pytest.raises(StateTransitionErrorV106):
        drill.verify(certificate(), NOW)
    future = certificate(generation=2, fingerprint="4" * 64, not_before=NOW + timedelta(minutes=1), not_after=NOW + timedelta(hours=1))
    with pytest.raises(ValidationErrorV106):
        drill.issue(future, NOW)

    expired_old = certificate(not_before=NOW - timedelta(hours=1), not_after=NOW - timedelta(minutes=20))
    new = certificate(generation=2, fingerprint="4" * 64, not_before=NOW - timedelta(minutes=10), not_after=NOW + timedelta(hours=1))
    overlap_drill = CertificateRenewalDrillV106("d2", policy(), expired_old, "a", "b")
    overlap_drill.issue(new, NOW)
    with pytest.raises(ValidationErrorV106, match="overlap"):
        overlap_drill.activate(NOW)

    valid_drill = CertificateRenewalDrillV106("d3", policy(), certificate(), "a", "b")
    valid_new = certificate(generation=2, fingerprint="4" * 64, not_before=NOW, not_after=NOW + timedelta(hours=1))
    valid_drill.issue(valid_new, NOW)
    with pytest.raises(StateTransitionErrorV106):
        valid_drill.issue(valid_new, NOW)


def test_backup_dr_and_bundle_remaining_branches():
    with pytest.raises(ValidationErrorV106):
        backup(created_at=NOW, completed_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationErrorV106):
        backup(encrypted=False)
    drill = DisasterRecoveryDrillV106("d", policy(), backup())
    with pytest.raises(StateTransitionErrorV106):
        drill.verify(restore(), NOW)
    drill.start(NOW)
    with pytest.raises(StateTransitionErrorV106):
        drill.start(NOW)
    future_restore = restore(started_at=NOW + timedelta(minutes=1), completed_at=NOW + timedelta(minutes=2))
    drill2 = DisasterRecoveryDrillV106("d2", policy(max_rpo_seconds=10000), backup())
    drill2.start(NOW)
    gates = drill2.verify(future_restore, NOW)
    assert "completion_freshness" in {g.name for g in gates.critical_failures}

    empty = DeploymentQualificationCoordinatorV106("q-empty", policy(), manifest(), {"key-1": SECRET})
    with pytest.raises(ValidationErrorV106):
        QualificationEvidenceBundleV106.from_coordinator(empty)
    with pytest.raises(ValidationErrorV106):
        QualificationEvidenceBundleV106("q", HEX_C, HEX_D, "e" * 64, (), "bad", "f" * 64, QualificationStateV106.PLANNED)
