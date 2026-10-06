from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.profile_registry import QualificationProfileRegistry
from app.qualification.profile_transparency import publish_profile_lifecycle
from app.qualification.signing_authority import QualificationSignatureReplayLedger
from app.qualification.transparency_log import QualificationTransparencyEntry
from app.qualification.trust_checkpoint_v4 import (
    build_trust_checkpoint_v4,
    sign_trust_checkpoint_v4,
    verify_trust_checkpoint_v4,
    verify_trust_checkpoint_v4_successor,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification import full_bundle


def active_profile_registry(profile) -> QualificationProfileRegistry:
    registry = QualificationProfileRegistry()
    registry.register(profile=profile, observed_at=NOW)
    registry.activate(
        profile_ref=profile.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    return registry


def v4_context():
    bundle, evidence_registry, log, root, signer, descriptor, keyring = full_bundle()
    profile_registry = active_profile_registry(bundle.profile)
    receipt = publish_profile_lifecycle(
        profile_registry=profile_registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=7),
    )
    return (
        bundle,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        root,
        signer,
        descriptor,
        keyring,
    )


def test_v4_binds_profile_state_history_and_publication_receipt() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()

    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )

    assert checkpoint.profile_record_count == profile_registry.record_count
    assert checkpoint.profile_state_root_sha256 == profile_registry.state_root_sha256
    assert checkpoint.profile_event_count == profile_registry.event_count
    assert checkpoint.profile_event_head_sha256 == profile_registry.event_head_sha256
    assert checkpoint.profile_publication_receipt_sha256 == receipt.receipt_sha256
    assert checkpoint.profile_publication_tree_size == receipt.transparency_tree_size
    assert checkpoint.profile_publication_root_sha256 == receipt.transparency_root_sha256
    assert checkpoint.transparency_tree_size == log.latest_head().tree_size
    assert checkpoint.checkpoint_id.startswith("qtrustv4_")


def test_v4_allows_global_transparency_growth_after_profile_publication() -> None:
    (
        bundle,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="OTHER_QUALIFICATION_ARTIFACT",
            object_id="other-after-profile-publication",
            object_sha256="f" * 64,
            subject=bundle.manifest.subject,
            subject_version=bundle.manifest.subject_version,
            profile_id=bundle.profile.profile_id,
            profile_version=bundle.profile.version,
            published_at=NOW + timedelta(seconds=8),
        ),
        issued_at=NOW + timedelta(seconds=8),
    )

    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=9),
    )

    assert checkpoint.transparency_tree_size > checkpoint.profile_publication_tree_size
    assert checkpoint.profile_publication_root_sha256 == receipt.transparency_root_sha256
    assert checkpoint.transparency_root_sha256 == log.latest_head().root_sha256


def test_v4_rejects_stale_profile_publication_after_lifecycle_mutation() -> None:
    (
        bundle,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    profile_registry.revoke(
        profile_ref=bundle.profile.profile_ref,
        reason="POLICY_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=9),
    )

    with pytest.raises(ValueError, match="receipt is not current and valid"):
        build_trust_checkpoint_v4(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            profile_publication_receipt=receipt,
            transparency_log=log,
            issued_at=NOW + timedelta(seconds=10),
        )


def test_v4_accepts_new_receipt_after_profile_lifecycle_suffix_publication() -> None:
    (
        bundle,
        evidence_registry,
        profile_registry,
        log,
        first_receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    first = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=first_receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )
    profile_registry.revoke(
        profile_ref=bundle.profile.profile_ref,
        reason="POLICY_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=9),
    )
    second_receipt = publish_profile_lifecycle(
        profile_registry=profile_registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=10),
    )
    second = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=second_receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=11),
        previous_checkpoint=first,
    )

    assert second.profile_event_count > first.profile_event_count
    assert second.profile_event_head_sha256 != first.profile_event_head_sha256
    assert (
        second.profile_publication_receipt_sha256
        != first.profile_publication_receipt_sha256
    )
    assert second.previous_trust_checkpoint_sha256 == first.checkpoint_sha256
    assert verify_trust_checkpoint_v4_successor(previous=first, current=second)


def test_v4_signature_verifies_and_replay_is_rejected() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        signer,
        descriptor,
        keyring,
    ) = v4_context()
    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )
    signed = sign_trust_checkpoint_v4(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="trust-v4-signature",
        issued_at=NOW + timedelta(seconds=8),
        expires_at=NOW + timedelta(minutes=10),
        nonce="trust-v4-nonce",
    )
    ledger = QualificationSignatureReplayLedger()

    verified = verify_trust_checkpoint_v4(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=9),
        replay_ledger=ledger,
    )

    assert verified.checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert verified.profile_event_head_sha256 == profile_registry.event_head_sha256
    assert verified.profile_publication_receipt_sha256 == receipt.receipt_sha256
    assert verified.signer_key_id == "qualification-key"

    with pytest.raises(ValueError, match="replay"):
        verify_trust_checkpoint_v4(
            signed_checkpoint=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=9),
            replay_ledger=ledger,
        )


def test_v4_rejects_tampered_profile_event_head_after_signing() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        signer,
        descriptor,
        keyring,
    ) = v4_context()
    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )
    signed = sign_trust_checkpoint_v4(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="trust-v4-tamper",
        issued_at=NOW + timedelta(seconds=8),
        expires_at=NOW + timedelta(minutes=10),
        nonce="trust-v4-tamper-nonce",
    )
    tampered = replace(
        signed,
        checkpoint=replace(
            checkpoint,
            profile_event_head_sha256="f" * 64,
        ),
    )

    with pytest.raises(ValueError, match="payload mismatch"):
        tampered.validate()


def test_v4_signature_cannot_predate_checkpoint() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        signer,
        descriptor,
        keyring,
    ) = v4_context()
    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )

    with pytest.raises(ValueError, match="signature cannot predate checkpoint"):
        sign_trust_checkpoint_v4(
            checkpoint=checkpoint,
            provider=signer,
            descriptor=descriptor,
            keyring_generation=keyring.generation,
            signature_id="trust-v4-early",
            issued_at=NOW + timedelta(seconds=7),
            expires_at=NOW + timedelta(minutes=1),
            nonce="trust-v4-early-nonce",
        )



def test_v4_payload_and_verified_payload_are_deterministic() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        signer,
        descriptor,
        keyring,
    ) = v4_context()
    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )
    assert checkpoint.payload() == checkpoint.payload()
    assert checkpoint.checkpoint_id == f"qtrustv4_{checkpoint.checkpoint_sha256[:24]}"

    signed = sign_trust_checkpoint_v4(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="trust-v4-payload",
        issued_at=NOW + timedelta(seconds=8),
        expires_at=NOW + timedelta(minutes=10),
        nonce="trust-v4-payload-nonce",
    )
    verified = verify_trust_checkpoint_v4(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=9),
    )
    assert verified.payload()["checkpoint_id"] == checkpoint.checkpoint_id
    assert verified.payload()["profile_event_count"] == profile_registry.event_count


def test_v4_validation_rejects_invalid_counts_and_tree_relationships() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )

    with pytest.raises(ValueError, match="evidence_event_count must be non-negative"):
        replace(checkpoint, evidence_event_count=-1).validate()

    with pytest.raises(ValueError, match="profile_event_count must be non-negative"):
        replace(checkpoint, profile_event_count=-1).validate()

    with pytest.raises(ValueError, match="cannot exceed transparency tree"):
        replace(
            checkpoint,
            profile_publication_tree_size=checkpoint.transparency_tree_size + 1,
        ).validate()


def test_v4_validation_rejects_invalid_genesis_invariants() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )

    with pytest.raises(ValueError, match="genesis head"):
        replace(
            checkpoint,
            evidence_event_count=0,
            evidence_event_head_sha256="f" * 64,
            evidence_state_root_sha256="0" * 64,
        ).validate()

    with pytest.raises(ValueError, match="profile event journal requires genesis head"):
        replace(
            checkpoint,
            profile_event_count=0,
            profile_event_head_sha256="f" * 64,
        ).validate()


def test_v4_rejects_backdated_checkpoint_against_publication_and_transparency() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()

    with pytest.raises(ValueError, match="predate profile publication receipt"):
        build_trust_checkpoint_v4(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            profile_publication_receipt=receipt,
            transparency_log=log,
            issued_at=receipt.observed_at - timedelta(seconds=1),
        )

    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="OTHER_QUALIFICATION_ARTIFACT",
            object_id="future-head-for-v4",
            object_sha256="e" * 64,
            subject="QUALIFICATION_TEST",
            subject_version="1",
            profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
            profile_version="1.0.0",
            published_at=NOW + timedelta(seconds=10),
        ),
        issued_at=NOW + timedelta(seconds=10),
    )
    with pytest.raises(ValueError, match="predate transparency head"):
        build_trust_checkpoint_v4(
            evidence_registry=evidence_registry,
            profile_registry=profile_registry,
            profile_publication_receipt=receipt,
            transparency_log=log,
            issued_at=NOW + timedelta(seconds=9),
        )


def test_v4_rejects_each_successor_counter_regression() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    current = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )

    cases = (
        (replace(current, evidence_event_count=current.evidence_event_count + 1),
         "evidence event count regression"),
        (replace(current, profile_record_count=current.profile_record_count + 1),
         "profile record count regression"),
        (replace(current, profile_event_count=current.profile_event_count + 1),
         "profile event count regression"),
        (replace(current, transparency_tree_size=current.transparency_tree_size + 1),
         "transparency size regression"),
    )
    for previous, message in cases:
        with pytest.raises(ValueError, match=message):
            build_trust_checkpoint_v4(
                evidence_registry=evidence_registry,
                profile_registry=profile_registry,
                profile_publication_receipt=receipt,
                transparency_log=log,
                issued_at=NOW + timedelta(seconds=9),
                previous_checkpoint=previous,
            )


def test_v4_successor_verifier_rejects_wrong_previous_checkpoint_link() -> None:
    (
        _,
        evidence_registry,
        profile_registry,
        log,
        receipt,
        _,
        _,
        _,
        _,
    ) = v4_context()
    first = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=8),
    )
    second = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=9),
        previous_checkpoint=first,
    )
    assert verify_trust_checkpoint_v4_successor(previous=first, current=second)
    assert not verify_trust_checkpoint_v4_successor(
        previous=first,
        current=replace(second, previous_trust_checkpoint_sha256="f" * 64),
    )
