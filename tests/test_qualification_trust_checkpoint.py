from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import QualificationEvidenceRegistry
from app.qualification.profile_registry import (
    QualificationProfileRegistry,
    QualificationProfileStatus,
)
from app.qualification.signing_authority import QualificationSignatureReplayLedger
from app.qualification.transparency_log import QualificationTransparencyLog
from app.qualification.trust_checkpoint import (
    build_trust_checkpoint,
    sign_trust_checkpoint,
    verify_trust_checkpoint,
    verify_trust_checkpoint_successor,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification import full_bundle


def active_profile_registry(profile):
    registry = QualificationProfileRegistry()
    registry.register(profile=profile, observed_at=NOW)
    registry.activate(
        profile_ref=profile.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    return registry


def test_trust_checkpoint_binds_both_registries_and_transparency() -> None:
    bundle, evidence_registry, log, _, _, _, _ = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    head = log.latest_head()

    checkpoint = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=head,
        issued_at=NOW + timedelta(seconds=7),
    )

    assert checkpoint.evidence_event_count == evidence_registry.event_count
    assert checkpoint.evidence_event_head_sha256 == evidence_registry.head_sha256
    assert checkpoint.evidence_state_root_sha256 == evidence_registry.state_root_sha256
    assert checkpoint.profile_record_count == profile_registry.record_count
    assert checkpoint.profile_state_root_sha256 == profile_registry.state_root_sha256
    assert checkpoint.transparency_tree_size == head.tree_size
    assert checkpoint.transparency_root_sha256 == head.root_sha256
    assert checkpoint.transparency_tree_head_sha256 == head.tree_head_sha256
    assert checkpoint.previous_trust_checkpoint_sha256 == "0" * 64
    assert checkpoint.checkpoint_id.startswith("qtrust_")


def test_trust_checkpoint_signature_verifies_and_reports_signer() -> None:
    bundle, evidence_registry, log, _, signer, descriptor, keyring = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    checkpoint = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )
    signed = sign_trust_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="combined-trust-checkpoint-1",
        issued_at=NOW + timedelta(seconds=7),
        expires_at=NOW + timedelta(minutes=10),
        nonce="combined-trust-checkpoint-nonce-1",
    )

    verified = verify_trust_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=8),
    )

    assert verified.checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert verified.evidence_state_root_sha256 == evidence_registry.state_root_sha256
    assert verified.profile_state_root_sha256 == profile_registry.state_root_sha256
    assert verified.signer_key_id == "qualification-key"
    assert verified.signer_owner_id == "qualification-owner"
    assert verified.keyring_generation == 1


def test_profile_revocation_changes_combined_trust_checkpoint() -> None:
    bundle, evidence_registry, log, _, _, _, _ = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    first = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )

    revoked = profile_registry.revoke(
        profile_ref=bundle.profile.profile_ref,
        reason="POLICY_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=8),
    )
    second = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=9),
        previous_checkpoint=first,
    )

    assert revoked.status is QualificationProfileStatus.REVOKED
    assert second.profile_state_root_sha256 != first.profile_state_root_sha256
    assert second.evidence_state_root_sha256 == first.evidence_state_root_sha256
    assert second.previous_trust_checkpoint_sha256 == first.checkpoint_sha256
    assert verify_trust_checkpoint_successor(previous=first, current=second)


def test_evidence_revocation_changes_combined_trust_checkpoint() -> None:
    bundle, evidence_registry, log, _, _, _, _ = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    first = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )

    evidence_registry.revoke(
        evidence_id=bundle.signed_evidence.evidence_id,
        reason="EVIDENCE_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=8),
    )
    second = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=9),
        previous_checkpoint=first,
    )

    assert second.evidence_state_root_sha256 != first.evidence_state_root_sha256
    assert second.evidence_event_count > first.evidence_event_count
    assert second.profile_state_root_sha256 == first.profile_state_root_sha256
    assert verify_trust_checkpoint_successor(previous=first, current=second)


def test_trust_checkpoint_rejects_backdated_state_and_counter_regression() -> None:
    bundle, evidence_registry, log, _, _, _, _ = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)

    profile_registry.revoke(
        profile_ref=bundle.profile.profile_ref,
        reason="POLICY_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=8),
    )
    with pytest.raises(ValueError, match="predate profile registry state"):
        build_trust_checkpoint(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=7),
        )

    current = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=9),
    )
    regressed = replace(
        current,
        evidence_event_count=current.evidence_event_count - 1,
        previous_trust_checkpoint_sha256=current.checkpoint_sha256,
        issued_at=NOW + timedelta(seconds=10),
    )
    assert not verify_trust_checkpoint_successor(previous=current, current=regressed)


def test_trust_checkpoint_rejects_signature_replay_and_payload_tamper() -> None:
    bundle, evidence_registry, log, _, signer, descriptor, keyring = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    checkpoint = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )
    signed = sign_trust_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="combined-trust-checkpoint-replay",
        issued_at=NOW + timedelta(seconds=7),
        expires_at=NOW + timedelta(minutes=10),
        nonce="combined-trust-checkpoint-replay-nonce",
    )
    ledger = QualificationSignatureReplayLedger()

    verify_trust_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=8),
        replay_ledger=ledger,
    )
    with pytest.raises(ValueError, match="replay"):
        verify_trust_checkpoint(
            signed_checkpoint=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=8),
            replay_ledger=ledger,
        )

    tampered = replace(
        signed,
        checkpoint=replace(
            checkpoint,
            profile_state_root_sha256="f" * 64,
        ),
    )
    with pytest.raises(ValueError, match="payload mismatch"):
        tampered.validate()


def test_empty_combined_trust_checkpoint_uses_genesis_roots() -> None:
    evidence_registry = QualificationEvidenceRegistry()
    profile_registry = QualificationProfileRegistry()
    log = QualificationTransparencyLog()

    checkpoint = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW,
    )

    assert checkpoint.evidence_event_count == 0
    assert checkpoint.evidence_event_head_sha256 == "0" * 64
    assert checkpoint.evidence_state_root_sha256 == "0" * 64
    assert checkpoint.profile_record_count == 0
    assert checkpoint.profile_state_root_sha256 == "0" * 64
    assert checkpoint.transparency_tree_size == 0
    assert checkpoint.transparency_root_sha256 == "0" * 64



def test_trust_checkpoint_rejects_backdated_evidence_and_transparency_state() -> None:
    bundle, evidence_registry, log, _, _, _, _ = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)

    with pytest.raises(ValueError, match="predate evidence registry state"):
        build_trust_checkpoint(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=1),
        )

    with pytest.raises(ValueError, match="predate transparency head"):
        build_trust_checkpoint(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=3),
        )


def test_trust_checkpoint_rejects_monotonic_counter_rollback() -> None:
    bundle, evidence_registry, log, _, _, _, _ = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    current = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )

    with pytest.raises(ValueError, match="evidence event count regression"):
        build_trust_checkpoint(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=8),
            previous_checkpoint=replace(
                current,
                evidence_event_count=current.evidence_event_count + 1,
            ),
        )

    with pytest.raises(ValueError, match="profile record count regression"):
        build_trust_checkpoint(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=8),
            previous_checkpoint=replace(
                current,
                profile_record_count=current.profile_record_count + 1,
            ),
        )

    with pytest.raises(ValueError, match="transparency size regression"):
        build_trust_checkpoint(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=8),
            previous_checkpoint=replace(
                current,
                transparency_tree_size=current.transparency_tree_size + 1,
            ),
        )


def test_trust_checkpoint_validation_rejects_invalid_genesis_invariants() -> None:
    evidence_registry = QualificationEvidenceRegistry()
    profile_registry = QualificationProfileRegistry()
    log = QualificationTransparencyLog()
    checkpoint = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW,
    )

    with pytest.raises(ValueError, match="genesis event head"):
        replace(
            checkpoint,
            evidence_event_head_sha256="f" * 64,
        ).validate()

    with pytest.raises(ValueError, match="profile registry requires genesis"):
        replace(
            checkpoint,
            profile_state_root_sha256="f" * 64,
        ).validate()

    with pytest.raises(ValueError, match="transparency log requires genesis"):
        replace(
            checkpoint,
            transparency_root_sha256="f" * 64,
        ).validate()


def test_trust_checkpoint_signature_cannot_predate_checkpoint() -> None:
    bundle, evidence_registry, log, _, signer, descriptor, keyring = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    checkpoint = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )

    with pytest.raises(ValueError, match="signature cannot predate checkpoint"):
        sign_trust_checkpoint(
            checkpoint=checkpoint,
            provider=signer,
            descriptor=descriptor,
            keyring_generation=keyring.generation,
            signature_id="combined-trust-checkpoint-early",
            issued_at=NOW + timedelta(seconds=6),
            expires_at=NOW + timedelta(minutes=1),
            nonce="combined-trust-checkpoint-early-nonce",
        )
