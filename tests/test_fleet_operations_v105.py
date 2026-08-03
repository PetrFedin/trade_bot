from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from app.runtime.fleet_operations_v105 import (
    ContainmentErrorV105,
    ContainmentScopeV105,
    ControlledAutoscalerV105,
    EnrollmentErrorV105,
    FleetEnrollmentAuthorityV105,
    FleetErrorV105,
    FleetEventJournalV105,
    FleetMetricsV105,
    FleetPolicyV105,
    FleetRegistryV105,
    KeyStatusV105,
    KubernetesAttestationV105,
    PolicyErrorV105,
    ReplayErrorV105,
    RotatingKeyRingV105,
    ScaleActionV105,
    SignatureErrorV105,
    SignedEnrollmentV105,
    SigningKeyV105,
    WorkerStateErrorV105,
    WorkerStateV105,
    readiness_snapshot,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def policy(**overrides):
    values = dict(
        fleet_id="fleet-a",
        generation=105,
        min_replicas=1,
        max_replicas=10,
        max_scale_up_step=3,
        max_scale_down_step=2,
        target_queue_per_worker=2,
        heartbeat_ttl=timedelta(seconds=30),
        enrollment_ttl=timedelta(minutes=5),
        drain_timeout=timedelta(seconds=20),
        scale_up_cooldown=timedelta(seconds=10),
        scale_down_cooldown=timedelta(seconds=20),
        stabilization_samples=2,
        crash_budget=1,
        dlq_budget=2,
        allowed_clusters=("cluster-a",),
        allowed_namespaces=("astra",),
        allowed_service_accounts=("astra-worker",),
        allowed_zones=("zone-a", "zone-b"),
        allowed_s3_hosts=("evidence.internal.example",),
        evidence_bucket="astra-evidence",
        evidence_prefix="fleet/a",
    )
    values.update(overrides)
    return FleetPolicyV105(**values)


def signing_key(key_id="key-1", secret=b"k" * 32, **overrides):
    values = dict(
        key_id=key_id,
        secret=secret,
        status=KeyStatusV105.ACTIVE,
        not_before=NOW - timedelta(hours=1),
        not_after=NOW + timedelta(days=1),
    )
    values.update(overrides)
    return SigningKeyV105(**values)


def attestation(**overrides):
    values = dict(
        cluster="cluster-a",
        namespace="astra",
        service_account="astra-worker",
        pod_uid="pod-1",
        node_uid="node-1",
        deployment_id="deploy-1",
        zone="zone-a",
        image_digest="sha256:" + "1" * 64,
        config_digest="2" * 64,
        audience="astra-worker-enrollment-v105",
    )
    values.update(overrides)
    return KubernetesAttestationV105(**values)


def stack(**policy_overrides):
    p = policy(**policy_overrides)
    ring = RotatingKeyRingV105([signing_key()])
    registry = FleetRegistryV105(p, ring)
    authority = FleetEnrollmentAuthorityV105(p, ring)
    return p, ring, registry, authority


def enroll(registry=None, authority=None, *, worker_id="worker-1", cert="a" * 64, att=None, now=NOW, token_id="token-1", nonce="nonce-1"):
    if registry is None or authority is None:
        _p, _ring, registry, authority = stack()
    att = att or attestation()
    token = authority.issue(
        token_id=token_id,
        worker_id=worker_id,
        attestation=att,
        certificate_fingerprint=cert,
        nonce=nonce,
        now=now,
    )
    worker = registry.enroll(token, att, now)
    return registry, authority, worker, token, att


@pytest.mark.parametrize(
    "field,value",
    [
        ("fleet_id", "bad space"),
        ("generation", 0),
        ("min_replicas", 0),
        ("max_replicas", 0),
        ("max_scale_up_step", 0),
        ("max_scale_down_step", 0),
        ("target_queue_per_worker", 0),
        ("stabilization_samples", 0),
        ("crash_budget", -1),
        ("dlq_budget", -1),
        ("heartbeat_ttl", timedelta(0)),
        ("enrollment_ttl", timedelta(0)),
        ("drain_timeout", timedelta(0)),
        ("scale_up_cooldown", timedelta(0)),
        ("scale_down_cooldown", timedelta(0)),
        ("allowed_clusters", ()),
        ("allowed_namespaces", ("",)),
        ("allowed_service_accounts", ("dup", "dup")),
        ("evidence_bucket", "Bad_Bucket"),
        ("evidence_prefix", "/root"),
        ("evidence_prefix", "a/../b"),
    ],
)
def test_policy_rejects_invalid(field, value):
    with pytest.raises(PolicyErrorV105):
        policy(**{field: value})


def test_policy_digest_is_stable_and_safety_flags_false():
    first = policy()
    second = policy()
    assert first.digest == second.digest
    assert len(first.digest) == 64
    assert first.external_order_routing_allowed is False
    assert first.live_trading_allowed is False


def test_signing_key_repr_redacts_secret_and_fingerprint_stable():
    key = signing_key()
    assert "kkkk" not in repr(key)
    assert key.fingerprint == hashlib.sha256(b"k" * 32).hexdigest()[:16]


@pytest.mark.parametrize("kwargs", [
    {"key_id": "bad key"},
    {"secret": b"short"},
    {"not_after": NOW - timedelta(days=2)},
])
def test_signing_key_validation(kwargs):
    with pytest.raises(PolicyErrorV105):
        signing_key(**kwargs)


def test_keyring_sign_verify_rotate_and_retiring_key_verifies():
    ring = RotatingKeyRingV105([signing_key()])
    key_id, signature = ring.sign(b"payload", NOW)
    ring.verify(key_id, b"payload", signature, NOW)
    new = signing_key("key-2", b"n" * 32)
    ring.rotate(new)
    snapshot = {item["key_id"]: item["status"] for item in ring.public_snapshot()}
    assert snapshot == {"key-1": "RETIRING", "key-2": "ACTIVE"}
    ring.verify("key-1", b"payload", signature, NOW)
    assert ring.active_key_id(NOW) == "key-2"


def test_keyring_duplicate_unknown_revoke_and_revoked_verify():
    ring = RotatingKeyRingV105([signing_key()])
    with pytest.raises(PolicyErrorV105):
        ring.add(signing_key())
    with pytest.raises(PolicyErrorV105):
        ring.revoke("missing")
    key_id, signature = ring.sign(b"payload", NOW)
    ring.revoke(key_id)
    with pytest.raises(SignatureErrorV105):
        ring.verify(key_id, b"payload", signature, NOW)


def test_keyring_rejects_multiple_or_missing_active_keys():
    ring = RotatingKeyRingV105([signing_key("a"), signing_key("b", b"b" * 32)])
    with pytest.raises(SignatureErrorV105):
        ring.sign(b"payload", NOW)
    with pytest.raises(SignatureErrorV105):
        ring.active_key_id(NOW)
    ring = RotatingKeyRingV105([signing_key(status=KeyStatusV105.RETIRING)])
    with pytest.raises(SignatureErrorV105):
        ring.sign(b"payload", NOW)


def test_keyring_rejects_invalid_rotation_and_signature_time():
    ring = RotatingKeyRingV105([signing_key()])
    with pytest.raises(PolicyErrorV105):
        ring.rotate(signing_key("retire", b"r" * 32, status=KeyStatusV105.RETIRING))
    with pytest.raises(PolicyErrorV105):
        ring.rotate(signing_key())
    key_id, signature = ring.sign(b"payload", NOW)
    with pytest.raises(SignatureErrorV105):
        ring.verify(key_id, b"tampered", signature, NOW)
    with pytest.raises(SignatureErrorV105):
        ring.verify(key_id, b"payload", signature, NOW + timedelta(days=2))


@pytest.mark.parametrize("field,value", [
    ("pod_uid", "bad id"),
    ("image_digest", "sha256:bad"),
    ("config_digest", "z" * 64),
])
def test_attestation_validation(field, value):
    with pytest.raises(PolicyErrorV105):
        attestation(**{field: value})


def test_enrollment_happy_path_and_journal_verifies():
    p, ring, registry, authority = stack()
    registry, authority, worker, token, att = enroll(registry, authority)
    assert worker.state is WorkerStateV105.ACTIVE
    assert worker.identity_generation == 1
    assert token.generation == p.generation
    assert token.attestation_digest
    registry.journal.verify()
    assert registry.journal.tail_digest != "0" * 64


def test_enrollment_signature_tamper_and_unknown_key_fail():
    _p, _ring, registry, authority = stack()
    att = attestation()
    token = authority.issue(token_id="token-1", worker_id="worker-1", attestation=att, certificate_fingerprint="a" * 64, nonce="nonce-1", now=NOW)
    with pytest.raises(SignatureErrorV105):
        registry.enroll(replace(token, signature="0" * 64), att, NOW)
    with pytest.raises(SignatureErrorV105):
        registry.enroll(replace(token, key_id="missing"), att, NOW)


def test_enrollment_replay_token_and_nonce_are_blocked():
    _p, _ring, registry, authority = stack()
    registry, authority, _worker, token, att = enroll(registry, authority)
    with pytest.raises(ReplayErrorV105):
        registry.enroll(token, att, NOW)
    other = authority.issue(token_id="token-2", worker_id="worker-2", attestation=attestation(pod_uid="pod-2"), certificate_fingerprint="b" * 64, nonce="nonce-1", now=NOW)
    with pytest.raises(ReplayErrorV105):
        registry.enroll(other, attestation(pod_uid="pod-2"), NOW)


def test_enrollment_time_generation_and_attestation_digest_boundaries():
    _p, _ring, registry, authority = stack()
    att = attestation()
    token = authority.issue(token_id="token", worker_id="worker", attestation=att, certificate_fingerprint="a" * 64, nonce="nonce", now=NOW, not_before=NOW + timedelta(seconds=5))
    with pytest.raises(EnrollmentErrorV105):
        registry.enroll(token, att, NOW)
    with pytest.raises(EnrollmentErrorV105):
        registry.enroll(token, att, NOW + timedelta(minutes=6))
    with pytest.raises(SignatureErrorV105):
        registry.enroll(replace(token, generation=104), att, NOW + timedelta(seconds=5))
    with pytest.raises(SignatureErrorV105):
        registry.enroll(replace(token, attestation_digest="0" * 64), att, NOW + timedelta(seconds=5))


@pytest.mark.parametrize("field,value,message", [
    ("cluster", "other", "cluster"),
    ("namespace", "other", "namespace"),
    ("service_account", "other", "service account"),
    ("zone", "other", "zone"),
    ("audience", "other", "audience"),
])
def test_enrollment_attestation_allowlists(field, value, message):
    _p, _ring, registry, authority = stack()
    att = attestation(**{field: value})
    token = authority.issue(token_id="token", worker_id="worker", attestation=att, certificate_fingerprint="a" * 64, nonce="nonce", now=NOW)
    with pytest.raises(EnrollmentErrorV105, match=message):
        registry.enroll(token, att, NOW)


def test_duplicate_active_worker_blocked_and_stopped_worker_can_reenroll():
    _p, _ring, registry, authority = stack()
    registry, authority, _worker, _token, att = enroll(registry, authority)
    second = authority.issue(token_id="token-2", worker_id="worker-1", attestation=att, certificate_fingerprint="b" * 64, nonce="nonce-2", now=NOW)
    with pytest.raises(EnrollmentErrorV105):
        registry.enroll(second, att, NOW)
    registry.begin_drain("worker-1", NOW)
    registry.finalize_drain("worker-1", evidence_flushed=True, now=NOW)
    worker = registry.enroll(second, att, NOW)
    assert worker.identity_generation == 2


def test_identity_rotation_requires_dual_control_and_revokes_old_cert():
    _p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    with pytest.raises(WorkerStateErrorV105):
        registry.rotate_identity(worker.worker_id, "b" * 64, operator_a="op", operator_b="op", now=NOW)
    rotated = registry.rotate_identity(worker.worker_id, "b" * 64, operator_a="op-a", operator_b="op-b", now=NOW)
    assert rotated.identity_generation == 2
    with pytest.raises(WorkerStateErrorV105):
        registry.heartbeat(worker.worker_id, "a" * 64, 1, NOW)
    registry.heartbeat(worker.worker_id, "b" * 64, 1, NOW)
    with pytest.raises(WorkerStateErrorV105):
        registry.rotate_identity(worker.worker_id, "b" * 64, operator_a="a", operator_b="b", now=NOW)


def test_revoke_is_idempotent_and_marks_recovery_when_claim_active():
    _p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    registry.assign_claim(worker.worker_id, NOW)
    revoked = registry.revoke_worker(worker.worker_id, reason="incident", operator_id="op", now=NOW)
    assert revoked.state is WorkerStateV105.REVOKED
    assert revoked.recovery_required is True
    assert registry.revoke_worker(worker.worker_id, reason="incident", operator_id="op", now=NOW) == revoked
    with pytest.raises(WorkerStateErrorV105):
        registry.heartbeat(worker.worker_id, revoked.certificate_fingerprint, 1, NOW)


def test_revoke_requires_reason_and_known_worker():
    _p, _ring, registry, _authority = stack()
    with pytest.raises(WorkerStateErrorV105):
        registry.revoke_worker("missing", reason="x", operator_id="op", now=NOW)
    _registry, _authority, worker, *_ = enroll(registry, _authority)
    with pytest.raises(WorkerStateErrorV105):
        registry.revoke_worker(worker.worker_id, reason="", operator_id="op", now=NOW)


def test_heartbeat_sequence_time_and_certificate_fences():
    _p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    updated = registry.heartbeat(worker.worker_id, worker.certificate_fingerprint, 1, NOW + timedelta(seconds=1))
    assert updated.heartbeat_sequence == 1
    with pytest.raises(WorkerStateErrorV105, match="sequence"):
        registry.heartbeat(worker.worker_id, worker.certificate_fingerprint, 1, NOW + timedelta(seconds=2))
    with pytest.raises(WorkerStateErrorV105, match="time"):
        registry.heartbeat(worker.worker_id, worker.certificate_fingerprint, 2, NOW)
    with pytest.raises(WorkerStateErrorV105, match="certificate"):
        registry.heartbeat(worker.worker_id, "b" * 64, 2, NOW + timedelta(seconds=2))


def test_claim_assignment_completion_staleness_and_containment():
    p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    assigned = registry.assign_claim(worker.worker_id, NOW)
    assert assigned.active_claims == 1
    completed = registry.complete_claim(worker.worker_id, NOW + timedelta(seconds=1))
    assert completed.active_claims == 0
    with pytest.raises(WorkerStateErrorV105):
        registry.complete_claim(worker.worker_id, NOW)
    with pytest.raises(WorkerStateErrorV105, match="stale"):
        registry.assert_claimable(worker.worker_id, NOW + p.heartbeat_ttl + timedelta(microseconds=1))
    registry.heartbeat(worker.worker_id, worker.certificate_fingerprint, 1, NOW + timedelta(seconds=1))
    registry.activate_containment("contain", ContainmentScopeV105.FLEET, p.fleet_id, reason="incident", now=NOW)
    with pytest.raises(ContainmentErrorV105):
        registry.assign_claim(worker.worker_id, NOW + timedelta(seconds=1))


def test_graceful_drain_success_idempotency_and_timeout_quarantine():
    p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    draining = registry.begin_drain(worker.worker_id, NOW)
    assert registry.begin_drain(worker.worker_id, NOW) == draining
    with pytest.raises(WorkerStateErrorV105):
        registry.assign_claim(worker.worker_id, NOW)
    with pytest.raises(WorkerStateErrorV105, match="not complete"):
        registry.finalize_drain(worker.worker_id, evidence_flushed=False, now=NOW + timedelta(seconds=1))
    stopped = registry.finalize_drain(worker.worker_id, evidence_flushed=True, now=NOW + timedelta(seconds=1))
    assert stopped.state is WorkerStateV105.STOPPED
    with pytest.raises(WorkerStateErrorV105):
        registry.begin_drain(worker.worker_id, NOW)

    _p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    registry.assign_claim(worker.worker_id, NOW)
    registry.begin_drain(worker.worker_id, NOW)
    quarantined = registry.finalize_drain(worker.worker_id, evidence_flushed=False, now=NOW + p.drain_timeout)
    assert quarantined.state is WorkerStateV105.QUARANTINED
    assert quarantined.recovery_required is True


def test_containment_scopes_idempotency_conflict_and_release():
    p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    for index, (scope, target) in enumerate([
        (ContainmentScopeV105.FLEET, p.fleet_id),
        (ContainmentScopeV105.ZONE, worker.zone),
        (ContainmentScopeV105.DEPLOYMENT, worker.deployment_id),
        (ContainmentScopeV105.WORKER, worker.worker_id),
    ], 1):
        record = registry.activate_containment(f"c-{index}", scope, target, reason="incident", now=NOW)
        assert registry.is_contained(worker)
        assert registry.activate_containment(f"c-{index}", scope, target, reason="incident", now=NOW) == record
    assert len(registry.active_containments()) == 4
    with pytest.raises(ContainmentErrorV105):
        registry.activate_containment("c-1", ContainmentScopeV105.WORKER, worker.worker_id, reason="different", now=NOW)
    with pytest.raises(ContainmentErrorV105):
        registry.release_containment("missing", operator_a="a", operator_b="b", cleanup_evidence_digest="0" * 64, cleanup_confirmed=True, now=NOW)
    with pytest.raises(ContainmentErrorV105):
        registry.release_containment("c-1", operator_a="a", operator_b="a", cleanup_evidence_digest="0" * 64, cleanup_confirmed=True, now=NOW)
    released = registry.release_containment("c-1", operator_a="a", operator_b="b", cleanup_evidence_digest="0" * 64, cleanup_confirmed=True, now=NOW)
    assert released.active is False
    assert registry.release_containment("c-1", operator_a="a", operator_b="b", cleanup_evidence_digest="0" * 64, cleanup_confirmed=True, now=NOW) == released


def test_containment_release_blocks_dirty_worker():
    p, _ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    registry.assign_claim(worker.worker_id, NOW)
    registry.activate_containment("c", ContainmentScopeV105.FLEET, p.fleet_id, reason="incident", now=NOW)
    with pytest.raises(ContainmentErrorV105, match="not clean"):
        registry.release_containment("c", operator_a="a", operator_b="b", cleanup_evidence_digest="0" * 64, cleanup_confirmed=True, now=NOW)


def test_event_journal_detects_tampering_and_empty_tail():
    journal = FleetEventJournalV105()
    assert journal.tail_digest == "0" * 64
    journal.append("A", NOW, details={"x": 1})
    journal.append("B", NOW + timedelta(seconds=1))
    journal.verify()
    journal._events[0] = replace(journal._events[0], digest="0" * 64)  # noqa: SLF001
    with pytest.raises(FleetErrorV105, match="digest"):
        journal.verify()


def metrics(**overrides):
    values = dict(
        queue_depth=0,
        current_replicas=2,
        ready_workers=2,
        active_claims=0,
        draining_workers=0,
        crash_count=0,
        dlq_depth=0,
        control_plane_ready=True,
        postgres_ready=True,
        object_store_ready=True,
        observed_at=NOW,
    )
    values.update(overrides)
    return FleetMetricsV105(**values)


@pytest.mark.parametrize("field", ["queue_depth", "current_replicas", "ready_workers", "active_claims", "draining_workers", "crash_count", "dlq_depth"])
def test_metrics_reject_negative(field):
    with pytest.raises(PolicyErrorV105):
        metrics(**{field: -1})


def test_autoscaler_scale_up_step_cooldown_and_chain():
    scaler = ControlledAutoscalerV105(policy())
    first = scaler.decide(metrics(queue_depth=20, current_replicas=2))
    assert first.action is ScaleActionV105.SCALE_UP
    assert first.desired_replicas == 5
    second = scaler.decide(metrics(queue_depth=20, current_replicas=5, observed_at=NOW + timedelta(seconds=1)))
    assert second.action is ScaleActionV105.HOLD
    assert second.reason == "SCALE_UP_COOLDOWN"
    assert second.previous_decision_digest == first.digest


def test_autoscaler_hold_safety_boundaries():
    for kwargs, expected in [
        ({"containment_active": True}, "CONTAINMENT_ACTIVE"),
        ({"control_plane_ready": False}, "DEPENDENCY_NOT_READY"),
        ({"postgres_ready": False}, "DEPENDENCY_NOT_READY"),
        ({"object_store_ready": False}, "DEPENDENCY_NOT_READY"),
        ({"crash_count": 2}, "INCIDENT_BUDGET_EXHAUSTED"),
        ({"dlq_depth": 3}, "INCIDENT_BUDGET_EXHAUSTED"),
    ]:
        scaler = ControlledAutoscalerV105(policy())
        containment = kwargs.pop("containment_active", False)
        decision = scaler.decide(metrics(queue_depth=20, **kwargs), containment_active=containment)
        assert decision.action is ScaleActionV105.HOLD
        assert decision.reason == expected


def test_autoscaler_scale_down_stabilization_cooldown_and_active_work():
    scaler = ControlledAutoscalerV105(policy())
    first = scaler.decide(metrics(queue_depth=0, current_replicas=6))
    assert first.reason == "SCALE_DOWN_STABILIZING"
    second = scaler.decide(metrics(queue_depth=0, current_replicas=6, observed_at=NOW + timedelta(seconds=1)))
    assert second.action is ScaleActionV105.SCALE_DOWN
    assert second.desired_replicas == 4
    third = scaler.decide(metrics(queue_depth=0, current_replicas=4, observed_at=NOW + timedelta(seconds=2)))
    assert third.reason == "SCALE_DOWN_COOLDOWN"

    scaler = ControlledAutoscalerV105(policy(stabilization_samples=1))
    protected = scaler.decide(metrics(queue_depth=0, current_replicas=6, active_claims=3))
    assert protected.reason == "ACTIVE_WORK_PROTECTS_CAPACITY"
    draining = scaler.decide(metrics(queue_depth=0, current_replicas=6, draining_workers=1, observed_at=NOW + timedelta(seconds=30)))
    assert draining.reason == "ACTIVE_WORK_PROTECTS_CAPACITY"


def test_autoscaler_stable_and_bounds():
    scaler = ControlledAutoscalerV105(policy(max_replicas=4))
    stable = scaler.decide(metrics(queue_depth=4, current_replicas=2))
    assert stable.reason == "TARGET_STABLE"
    capped = scaler.decide(metrics(queue_depth=100, current_replicas=2, observed_at=NOW + timedelta(seconds=30)))
    assert capped.raw_desired_replicas == 4
    assert capped.desired_replicas == 4


def test_readiness_snapshot_healthy_stale_contained_and_quarantined():
    p, ring, registry, authority = stack()
    registry, authority, worker, *_ = enroll(registry, authority)
    healthy = readiness_snapshot(p, ring, registry, NOW)
    assert healthy.eligible_for_read_only_fleet is True
    assert healthy.external_order_routing_allowed is False
    assert healthy.live_trading_allowed is False
    stale = readiness_snapshot(p, ring, registry, NOW + p.heartbeat_ttl + timedelta(seconds=1))
    assert stale.stale_workers == 1
    assert stale.eligible_for_read_only_fleet is False
    registry.activate_containment("c", ContainmentScopeV105.FLEET, p.fleet_id, reason="x", now=NOW)
    contained = readiness_snapshot(p, ring, registry, NOW)
    assert contained.active_containments == 1
    assert contained.eligible_for_read_only_fleet is False


def test_naive_datetime_is_rejected():
    p, ring, registry, authority = stack()
    with pytest.raises(PolicyErrorV105):
        authority.issue(token_id="t", worker_id="w", attestation=attestation(), certificate_fingerprint="a" * 64, nonce="n", now=datetime(2026, 1, 1))
    with pytest.raises(PolicyErrorV105):
        readiness_snapshot(p, ring, registry, datetime(2026, 1, 1))
