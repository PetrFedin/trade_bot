from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import (
    EvidenceVerificationStatus,
    QualificationEvidenceRegistry,
)
from app.qualification.portable_verification import (
    PortableQualificationVerificationBundle,
    verify_portable_qualification_bundle,
)
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.profile_registry import QualificationProfile
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.registry_checkpoint import (
    build_registry_checkpoint,
    sign_registry_checkpoint,
)
from app.qualification.signed_evidence import sign_qualification_binding
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSigningKeyDescriptor,
    verify_qualification_keyring,
)
from app.qualification.transparency_log import (
    QualificationTransparencyEntry,
    QualificationTransparencyLog,
)
from app.runtime.signing_authority_v108 import SigningBackendV108
from tests.helpers_v108 import NOW, LocalProviderV108


def authority():
    root = LocalProviderV108.create("qualification-root", SigningBackendV108.HSM)
    signer = LocalProviderV108.create("qualification-key", SigningBackendV108.HSM)
    descriptor = QualificationSigningKeyDescriptor(
        key_id=signer.key_id,
        owner_id="qualification-owner",
        backend="HSM",
        generation=1,
        public_key_b64=b64encode(signer.public_key_bytes()).decode("ascii"),
        not_before=NOW - timedelta(hours=1),
        not_after=NOW + timedelta(hours=1),
    )
    snapshot = QualificationKeyringSnapshot.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(descriptor,),
        root_provider=root,
    )
    keyring = verify_qualification_keyring(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )
    return root, signer, descriptor, snapshot, keyring


def profile() -> QualificationProfile:
    value = QualificationProfile(
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        version="1.0.0",
        scope="PUBLIC_MARKET_DATA_ONLY",
        allowed_environments=("mainnet-public-readonly",),
        required_corpus_classes=("BYBIT_PUBLIC_KLINE",),
        required_checks=("BYBIT-PMD-001-SUBSCRIPTION-IDENTITY",),
        required_assertions=("ADAPTER_CONFORMANCE_PASS",),
        required_limitations=("PROFITABILITY_NOT_PROVEN",),
        created_at=NOW - timedelta(seconds=1),
    )
    value.validate()
    return value


def evidence_artifact(
    *,
    version: str,
    suffix: str,
    policy: QualificationProfile,
    signer,
    descriptor,
    keyring,
):
    manifest = QualificationManifest(
        job_id=f"qjob_{version:0>24}",
        job_result_sha256="1" * 64,
        request_sha256="2" * 64,
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version=f"build-{version}",
        profile_id=policy.profile_id,
        profile_version=policy.version,
        environment="mainnet-public-readonly",
        corpus_ids=(f"BYBIT:BTCUSDT:{version}",),
        provider_replay_sha256="3" * 64,
        adapter_conformance_sha256="4" * 64,
        marketdata_integrity_sha256="5" * 64,
        continuity_checkpoint_id=f"continuity-{version}",
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        scope=policy.scope,
        assertions=(
            "PROVIDER_COMPLETE_REPLAY_BOUND",
            "ADAPTER_CONFORMANCE_PASS",
            "MARKET_DATA_INTEGRITY_HEALTHY",
            "READ_ONLY_PUBLIC_MARKETDATA_SCOPE",
        ),
        limitations=(
            "EXECUTION_ORDER_SUBMIT_NOT_QUALIFIED",
            "EXECUTION_CANCEL_REPLACE_NOT_QUALIFIED",
            "EXECUTION_FILL_STATUS_MAPPING_NOT_QUALIFIED",
            "ACCOUNT_RECONCILIATION_NOT_QUALIFIED",
            "PROFITABILITY_NOT_PROVEN",
            "REGULATORY_CERTIFICATION_NOT_CLAIMED",
        ),
        issued_at=NOW,
    )
    manifest.validate()
    binding = ProfileBoundQualificationManifest(
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        profile_id=policy.profile_id,
        profile_version=policy.version,
        profile_sha256=policy.profile_sha256,
        scope=policy.scope,
        environment=manifest.environment,
    )
    binding.validate()
    signed = sign_qualification_binding(
        binding=binding,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id=f"qualification-{suffix}",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        nonce=f"qualification-nonce-{suffix}",
    )
    return manifest, binding, signed


def full_bundle():
    root, signer, descriptor, snapshot, keyring = authority()
    policy = profile()
    registry = QualificationEvidenceRegistry()
    log = QualificationTransparencyLog()

    first = evidence_artifact(
        version="1",
        suffix="first",
        policy=policy,
        signer=signer,
        descriptor=descriptor,
        keyring=keyring,
    )
    second = evidence_artifact(
        version="2",
        suffix="second",
        policy=policy,
        signer=signer,
        descriptor=descriptor,
        keyring=keyring,
    )

    for index, artifact in enumerate((first, second), start=1):
        manifest, binding, signed = artifact
        registry.register(
            manifest=manifest,
            binding=binding,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=index),
        )
        log.append(
            entry=QualificationTransparencyEntry(
                entry_type="SIGNED_QUALIFICATION_EVIDENCE",
                object_id=signed.evidence_id,
                object_sha256=signed.envelope.envelope_sha256,
                subject=manifest.subject,
                subject_version=manifest.subject_version,
                profile_id=manifest.profile_id,
                profile_version=manifest.profile_version,
                published_at=NOW + timedelta(seconds=index + 2),
            ),
            issued_at=NOW + timedelta(seconds=index + 2),
        )

    manifest, binding, signed = first
    decision = registry.verify(
        evidence_id=signed.evidence_id,
        observed_at=NOW + timedelta(seconds=5),
    )
    state_proof = registry.state_proof(evidence_id=signed.evidence_id)
    entry = next(
        item for item in log.entries() if item.object_id == signed.evidence_id
    )
    inclusion = log.inclusion_proof(object_id=signed.evidence_id)
    head = log.latest_head()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=head,
        issued_at=NOW + timedelta(seconds=6),
    )
    signed_checkpoint = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-checkpoint",
        issued_at=NOW + timedelta(seconds=6),
        expires_at=NOW + timedelta(minutes=10),
        nonce="portable-checkpoint-nonce",
    )
    bundle = PortableQualificationVerificationBundle(
        profile=policy,
        manifest=manifest,
        binding=binding,
        signed_evidence=signed,
        registry_decision=decision,
        registry_state_proof=state_proof,
        transparency_entry=entry,
        transparency_inclusion_proof=inclusion,
        transparency_head=head,
        signed_registry_checkpoint=signed_checkpoint,
        keyring_snapshot=snapshot,
    )
    bundle.validate()
    return bundle, registry, log, root, signer, descriptor, keyring


def test_portable_bundle_verifies_from_external_trust_root_only() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    result = verify_portable_qualification_bundle(
        bundle=bundle,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=7),
    )

    assert result.bundle_id == bundle.bundle_id
    assert result.bundle_sha256 == bundle.bundle_sha256
    assert result.lifecycle_status is EvidenceVerificationStatus.VALID
    assert result.evidence_id == bundle.signed_evidence.evidence_id
    assert result.binding_id == bundle.binding.binding_id
    assert result.profile_ref == bundle.profile.profile_ref
    assert result.registry_state_root_sha256 == (
        bundle.registry_state_proof.state_root_sha256
    )
    assert result.transparency_root_sha256 == bundle.transparency_head.root_sha256
    assert result.evidence_signer_key_id == "qualification-key"
    assert result.checkpoint_signer_key_id == "qualification-key"


def test_portable_bundle_rejects_untrusted_root_and_keyring_rollback() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    with pytest.raises(ValueError, match="untrusted"):
        verify_portable_qualification_bundle(
            bundle=bundle,
            trusted_root_public_keys={},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )

    with pytest.raises(ValueError, match="not monotonic"):
        verify_portable_qualification_bundle(
            bundle=bundle,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=1,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_profile_or_manifest_substitution() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    tampered_profile = replace(
        bundle,
        profile=replace(bundle.profile, version="9.9.9"),
    )
    with pytest.raises(ValueError, match="profile_version mismatch"):
        verify_portable_qualification_bundle(
            bundle=tampered_profile,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )

    tampered_manifest = replace(
        bundle,
        manifest=replace(bundle.manifest, organisation_id="OTHER"),
    )
    with pytest.raises(
        ValueError,
        match="binding manifest id mismatch|binding manifest digest mismatch",
    ):
        verify_portable_qualification_bundle(
            bundle=tampered_manifest,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_tampered_registry_state_proof() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    assert bundle.registry_state_proof.audit_path

    tampered = replace(
        bundle,
        registry_state_proof=replace(
            bundle.registry_state_proof,
            audit_path=("f" * 64, *bundle.registry_state_proof.audit_path[1:]),
        ),
    )

    with pytest.raises(ValueError, match="state proof is invalid"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_tampered_transparency_inclusion() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    assert bundle.transparency_inclusion_proof.audit_path

    tampered = replace(
        bundle,
        transparency_inclusion_proof=replace(
            bundle.transparency_inclusion_proof,
            audit_path=(
                "f" * 64,
                *bundle.transparency_inclusion_proof.audit_path[1:],
            ),
        ),
    )

    with pytest.raises(ValueError, match="transparency inclusion proof is invalid"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_revoked_evidence_remains_crypto_valid_but_lifecycle_is_revoked() -> None:
    bundle, registry, log, root, signer, descriptor, keyring = full_bundle()
    evidence_id = bundle.signed_evidence.evidence_id

    registry.revoke(
        evidence_id=evidence_id,
        reason="QUALIFICATION_RETRACTED",
        observed_at=NOW + timedelta(seconds=8),
    )
    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="QUALIFICATION_REVOCATION",
            object_id=f"revocation-{evidence_id}",
            object_sha256=registry.head_sha256,
            subject=bundle.manifest.subject,
            subject_version=bundle.manifest.subject_version,
            profile_id=bundle.manifest.profile_id,
            profile_version=bundle.manifest.profile_version,
            published_at=NOW + timedelta(seconds=9),
        ),
        issued_at=NOW + timedelta(seconds=9),
    )
    decision = registry.verify(
        evidence_id=evidence_id,
        observed_at=NOW + timedelta(seconds=10),
    )
    state_proof = registry.state_proof(evidence_id=evidence_id)
    inclusion = log.inclusion_proof(object_id=evidence_id)
    head = log.latest_head()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=head,
        issued_at=NOW + timedelta(seconds=11),
        previous_checkpoint=bundle.signed_registry_checkpoint.checkpoint,
    )
    signed_checkpoint = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-checkpoint-revoked",
        issued_at=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(minutes=10),
        nonce="portable-checkpoint-revoked-nonce",
    )
    revoked_bundle = replace(
        bundle,
        registry_decision=decision,
        registry_state_proof=state_proof,
        transparency_inclusion_proof=inclusion,
        transparency_head=head,
        signed_registry_checkpoint=signed_checkpoint,
    )

    result = verify_portable_qualification_bundle(
        bundle=revoked_bundle,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=12),
    )

    assert result.lifecycle_status is EvidenceVerificationStatus.REVOKED


def test_new_registry_state_cannot_be_paired_with_stale_checkpoint() -> None:
    bundle, registry, _, root, _, _, _ = full_bundle()
    registry.revoke(
        evidence_id=bundle.signed_evidence.evidence_id,
        reason="QUALIFICATION_RETRACTED",
        observed_at=NOW + timedelta(seconds=8),
    )
    stale = replace(
        bundle,
        registry_decision=registry.verify(
            evidence_id=bundle.signed_evidence.evidence_id,
            observed_at=NOW + timedelta(seconds=9),
        ),
        registry_state_proof=registry.state_proof(
            evidence_id=bundle.signed_evidence.evidence_id
        ),
    )

    with pytest.raises(
        ValueError,
        match="registry event head mismatch|checkpoint state root mismatch",
    ):
        verify_portable_qualification_bundle(
            bundle=stale,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=9),
        )



def test_portable_bundle_payload_and_identity_are_deterministic() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    first_payload = bundle.payload()
    second_payload = bundle.payload()

    assert first_payload == second_payload
    assert bundle.bundle_sha256 == bundle.bundle_sha256
    assert bundle.bundle_id == f"qverify_{bundle.bundle_sha256[:24]}"

    result = verify_portable_qualification_bundle(
        bundle=bundle,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=7),
    )
    assert result.payload()["bundle_id"] == bundle.bundle_id
    assert result.payload()["lifecycle_status"] == "VALID"


def test_portable_bundle_rejects_profile_scope_and_environment_drift() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    wrong_scope = replace(
        bundle,
        profile=replace(bundle.profile, scope="EXECUTION"),
    )
    with pytest.raises(ValueError, match="profile scope mismatch"):
        verify_portable_qualification_bundle(
            bundle=wrong_scope,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )

    wrong_environment = replace(
        bundle,
        profile=replace(
            bundle.profile,
            allowed_environments=("testnet",),
        ),
    )
    with pytest.raises(ValueError, match="environment not allowed"):
        verify_portable_qualification_bundle(
            bundle=wrong_environment,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_signed_profile_digest_drift() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    tampered = replace(
        bundle,
        signed_evidence=replace(
            bundle.signed_evidence,
            profile_sha256="f" * 64,
        ),
    )

    with pytest.raises(ValueError, match="signed profile digest mismatch"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_registry_decision_root_drift() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    tampered = replace(
        bundle,
        registry_decision=replace(
            bundle.registry_decision,
            state_root_sha256="f" * 64,
        ),
    )

    with pytest.raises(ValueError, match="registry state root mismatch"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_transparency_root_and_size_drift() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    wrong_root = replace(
        bundle,
        transparency_head=replace(
            bundle.transparency_head,
            root_sha256="f" * 64,
        ),
    )
    with pytest.raises(ValueError, match="transparency proof root mismatch"):
        verify_portable_qualification_bundle(
            bundle=wrong_root,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )

    wrong_size = replace(
        bundle,
        transparency_head=replace(
            bundle.transparency_head,
            tree_size=bundle.transparency_head.tree_size + 1,
        ),
    )
    with pytest.raises(ValueError, match="transparency proof size mismatch"):
        verify_portable_qualification_bundle(
            bundle=wrong_size,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_bundle_rejects_keyring_generation_drift() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    evidence_generation = replace(
        bundle,
        signed_evidence=replace(
            bundle.signed_evidence,
            envelope=replace(
                bundle.signed_evidence.envelope,
                keyring_generation=bundle.keyring_snapshot.generation + 1,
            ),
        ),
    )
    with pytest.raises(ValueError, match="registry evidence id mismatch"):
        verify_portable_qualification_bundle(
            bundle=evidence_generation,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )

    checkpoint_generation = replace(
        bundle,
        signed_registry_checkpoint=replace(
            bundle.signed_registry_checkpoint,
            envelope=replace(
                bundle.signed_registry_checkpoint.envelope,
                keyring_generation=bundle.keyring_snapshot.generation + 1,
            ),
        ),
    )
    with pytest.raises(ValueError, match="checkpoint keyring generation mismatch"):
        verify_portable_qualification_bundle(
            bundle=checkpoint_generation,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )


def test_portable_verifier_rejects_lifecycle_decision_mismatch() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    mismatched = replace(
        bundle,
        registry_decision=replace(
            bundle.registry_decision,
            status=EvidenceVerificationStatus.REVOKED,
            reason="FORGED_REVOKE",
        ),
    )
    mismatched.registry_decision.validate()

    with pytest.raises(ValueError, match="lifecycle decision mismatch"):
        verify_portable_qualification_bundle(
            bundle=mismatched,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=7),
        )
