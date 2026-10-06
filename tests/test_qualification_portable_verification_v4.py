from __future__ import annotations

import base64
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.portable_verification_v4 import (
    PortableQualificationVerificationBundleV4,
    PortableQualificationVerificationErrorV4,
    PortableVerificationFailureCodeV4,
    verify_portable_qualification_bundle_v4,
)
from app.qualification.profile_event_delta import build_profile_event_delta
from app.qualification.profile_transparency import (
    profile_event_object_id,
    publish_profile_lifecycle,
)
from app.qualification.transparency_log import QualificationTransparencyEntry
from app.qualification.trust_checkpoint_v4 import (
    build_trust_checkpoint_v4,
    sign_trust_checkpoint_v4,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification_v3 import bundle_v3


def bundle_v4():
    (
        base,
        evidence_registry,
        profile_registry,
        log,
        root,
        signer,
        descriptor,
        keyring,
    ) = bundle_v3()

    delta = build_profile_event_delta(
        profile_registry=profile_registry,
        previous_event_count=0,
    )
    receipt = publish_profile_lifecycle(
        profile_registry=profile_registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=8),
    )
    publication_head = log.head_at_size(receipt.transparency_tree_size)

    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="OTHER_QUALIFICATION_ARTIFACT",
            object_id="portable-v4-later-artifact",
            object_sha256="d" * 64,
            subject="QUALIFICATION_TEST",
            subject_version="1",
            profile_id=base.profile.profile_id,
            profile_version=base.profile.version,
            published_at=NOW + timedelta(seconds=9),
        ),
        issued_at=NOW + timedelta(seconds=9),
    )
    current_head = log.latest_head()
    consistency = log.consistency_proof(
        previous_tree_size=base.transparency_head.tree_size,
    )

    entries_by_id = {entry.object_id: entry for entry in log.entries()}
    profile_entries = tuple(
        entries_by_id[profile_event_object_id(event)]
        for event in delta.appended_events
    )
    profile_proofs = tuple(
        log.inclusion_proof(object_id=entry.object_id)
        for entry in profile_entries
    )

    checkpoint = build_trust_checkpoint_v4(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        profile_publication_receipt=receipt,
        transparency_log=log,
        issued_at=NOW + timedelta(seconds=10),
    )
    signed = sign_trust_checkpoint_v4(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-v4-trust",
        issued_at=NOW + timedelta(seconds=10),
        expires_at=NOW + timedelta(minutes=10),
        nonce="portable-v4-trust-nonce",
    )

    value = PortableQualificationVerificationBundleV4(
        base_v3=base,
        profile_event_delta=delta,
        profile_event_entries=profile_entries,
        profile_event_inclusion_proofs=profile_proofs,
        profile_publication_receipt=receipt,
        profile_publication_head=publication_head,
        transparency_consistency_proof=consistency,
        current_transparency_head=current_head,
        signed_trust_checkpoint_v4=signed,
    )
    value.validate()
    return value, root


def verify(
    bundle,
    root,
    *,
    count=0,
    head="0" * 64,
    transparency_size=None,
    transparency_root=None,
    checkpoint_sha="0" * 64,
):
    if transparency_size is None:
        transparency_size = bundle.base_v3.transparency_head.tree_size
    if transparency_root is None:
        transparency_root = bundle.base_v3.transparency_head.root_sha256
    return verify_portable_qualification_bundle_v4(
        bundle=bundle,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        trusted_previous_profile_event_count=count,
        trusted_previous_profile_event_head_sha256=head,
        trusted_previous_transparency_tree_size=transparency_size,
        trusted_previous_transparency_root_sha256=transparency_root,
        trusted_previous_checkpoint_v4_sha256=checkpoint_sha,
        observed_at=NOW + timedelta(seconds=11),
    )


def test_portable_v4_verifies_incremental_profile_history_and_publication() -> None:
    bundle, root = bundle_v4()

    result = verify(bundle, root)

    assert result.usable
    assert result.bundle_id.startswith("qverifyv4_")
    assert result.current_profile_event_count == bundle.profile_event_delta.current_event_count
    assert result.current_profile_event_head_sha256 == (
        bundle.profile_event_delta.current_event_head_sha256
    )
    assert result.appended_profile_event_count == len(
        bundle.profile_event_delta.appended_events
    )
    assert result.profile_publication_receipt_sha256 == (
        bundle.profile_publication_receipt.receipt_sha256
    )
    assert result.current_transparency_tree_size == (
        bundle.current_transparency_head.tree_size
    )
    assert result.current_transparency_root_sha256 == (
        bundle.current_transparency_head.root_sha256
    )
    assert result.base_v3_result.usable


def test_portable_v4_rejects_wrong_external_profile_anchor() -> None:
    bundle, root = bundle_v4()

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(bundle, root, count=1, head="f" * 64)

    assert caught.value.code is PortableVerificationFailureCodeV4.PROFILE_DELTA_REJECTED


def test_portable_v4_rejects_tampered_profile_event_inclusion() -> None:
    bundle, root = bundle_v4()
    proof = bundle.profile_event_inclusion_proofs[0]
    assert proof.audit_path
    tampered = replace(
        bundle,
        profile_event_inclusion_proofs=(
            replace(
                proof,
                audit_path=("f" * 64, *proof.audit_path[1:]),
            ),
            *bundle.profile_event_inclusion_proofs[1:],
        ),
    )

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(tampered, root)

    assert (
        caught.value.code
        is PortableVerificationFailureCodeV4.PROFILE_PUBLICATION_REJECTED
    )


def test_portable_v4_rejects_transparency_fork_after_publication_head() -> None:
    bundle, root = bundle_v4()
    proof = bundle.transparency_consistency_proof
    assert proof.appended_leaf_hashes
    tampered_hashes = (
        *proof.appended_leaf_hashes[:-1],
        "f" * 64,
    )
    tampered = replace(
        bundle,
        transparency_consistency_proof=replace(
            proof,
            appended_leaf_hashes=tampered_hashes,
        ),
    )

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(tampered, root)

    assert (
        caught.value.code
        is PortableVerificationFailureCodeV4.TRANSPARENCY_CONTINUITY_REJECTED
    )


def test_portable_v4_rejects_tampered_checkpoint_signature() -> None:
    bundle, root = bundle_v4()
    raw = bytearray(
        base64.b64decode(bundle.signed_trust_checkpoint_v4.envelope.signature_b64)
    )
    raw[0] ^= 1
    tampered = replace(
        bundle,
        signed_trust_checkpoint_v4=replace(
            bundle.signed_trust_checkpoint_v4,
            envelope=replace(
                bundle.signed_trust_checkpoint_v4.envelope,
                signature_b64=base64.b64encode(bytes(raw)).decode("ascii"),
            ),
        ),
    )

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(tampered, root)

    assert (
        caught.value.code
        is PortableVerificationFailureCodeV4.TRUST_CHECKPOINT_V4_REJECTED
    )


def test_portable_v4_rejects_semantic_profile_event_entry_tamper() -> None:
    bundle, root = bundle_v4()
    entry = bundle.profile_event_entries[0]
    tampered = replace(
        bundle,
        profile_event_entries=(
            replace(entry, subject="OTHER_SUBJECT"),
            *bundle.profile_event_entries[1:],
        ),
    )

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(tampered, root)

    assert caught.value.code is PortableVerificationFailureCodeV4.BUNDLE_INVALID
    assert "subject mismatch" in caught.value.detail


def test_portable_v4_rejects_base_v3_trust_failure() -> None:
    bundle, _ = bundle_v4()

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify_portable_qualification_bundle_v4(
            bundle=bundle,
            trusted_root_public_keys={"wrong-root": b"x" * 32},
            previous_keyring_generation=0,
            trusted_previous_profile_event_count=0,
            trusted_previous_profile_event_head_sha256="0" * 64,
            trusted_previous_transparency_tree_size=(
                bundle.base_v3.transparency_head.tree_size
            ),
            trusted_previous_transparency_root_sha256=(
                bundle.base_v3.transparency_head.root_sha256
            ),
            trusted_previous_checkpoint_v4_sha256="0" * 64,
            observed_at=NOW + timedelta(seconds=11),
        )

    assert caught.value.code is PortableVerificationFailureCodeV4.BASE_V3_REJECTED


def test_portable_v4_payload_and_identity_are_deterministic() -> None:
    bundle, root = bundle_v4()

    assert bundle.payload() == bundle.payload()
    assert bundle.bundle_id == f"qverifyv4_{bundle.bundle_sha256[:24]}"

    result = verify(bundle, root)
    assert result.payload()["bundle_id"] == bundle.bundle_id
    assert result.payload()["usable"] is True



def test_portable_v4_rejects_wrong_previous_transparency_anchor() -> None:
    bundle, root = bundle_v4()

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(
            bundle,
            root,
            transparency_root="f" * 64,
        )

    assert (
        caught.value.code
        is PortableVerificationFailureCodeV4.TRANSPARENCY_CONTINUITY_REJECTED
    )


def test_portable_v4_rejects_wrong_previous_checkpoint_anchor() -> None:
    bundle, root = bundle_v4()

    with pytest.raises(PortableQualificationVerificationErrorV4) as caught:
        verify(
            bundle,
            root,
            checkpoint_sha="f" * 64,
        )

    assert caught.value.code is PortableVerificationFailureCodeV4.V4_BINDING_MISMATCH


def test_portable_v4_result_exposes_next_round_trust_anchors() -> None:
    bundle, root = bundle_v4()

    result = verify(bundle, root)

    assert result.checkpoint_v4_sha256 == (
        bundle.signed_trust_checkpoint_v4.checkpoint.checkpoint_sha256
    )
    assert result.current_transparency_tree_size == (
        bundle.current_transparency_head.tree_size
    )
    assert result.current_transparency_root_sha256 == (
        bundle.current_transparency_head.root_sha256
    )
    assert result.current_profile_event_count == (
        bundle.profile_event_delta.current_event_count
    )
    assert result.current_profile_event_head_sha256 == (
        bundle.profile_event_delta.current_event_head_sha256
    )
