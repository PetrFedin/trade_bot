from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import QualificationEvidenceRegistry
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.registry_checkpoint import (
    build_registry_checkpoint,
    sign_registry_checkpoint,
    verify_checkpoint_successor,
    verify_registry_checkpoint,
)
from app.qualification.signed_evidence import sign_qualification_binding
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSignatureReplayLedger,
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
    return signer, descriptor, keyring


def evidence_bundle(*, version: str, suffix: str):
    signer, descriptor, keyring = authority()
    manifest = QualificationManifest(
        job_id=f"qjob_{version:0>24}",
        job_result_sha256="1" * 64,
        request_sha256="2" * 64,
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version=f"build-{version}",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
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
        scope="PUBLIC_MARKET_DATA_ONLY",
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
        profile_id=manifest.profile_id,
        profile_version=manifest.profile_version,
        profile_sha256="6" * 64,
        scope=manifest.scope,
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
        expires_at=NOW + timedelta(minutes=2),
        nonce=f"qualification-nonce-{suffix}",
    )
    return manifest, binding, signed, keyring, signer, descriptor


def populated_registry_and_log():
    registry = QualificationEvidenceRegistry()
    log = QualificationTransparencyLog()
    manifest, binding, signed, keyring, signer, descriptor = evidence_bundle(
        version="1",
        suffix="one",
    )
    record = registry.register(
        manifest=manifest,
        binding=binding,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
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
            published_at=NOW + timedelta(seconds=2),
        ),
        issued_at=NOW + timedelta(seconds=2),
    )
    return registry, log, record, keyring, signer, descriptor


def test_checkpoint_binds_registry_state_and_transparency_head() -> None:
    registry, log, _, _, _, _ = populated_registry_and_log()
    transparency_head = log.latest_head()

    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=transparency_head,
        issued_at=NOW + timedelta(seconds=3),
    )

    assert checkpoint.registry_event_count == 1
    assert checkpoint.registry_event_head_sha256 == registry.head_sha256
    assert checkpoint.registry_state_root_sha256 == registry.state_root_sha256
    assert checkpoint.transparency_tree_size == transparency_head.tree_size
    assert checkpoint.transparency_root_sha256 == transparency_head.root_sha256
    assert (
        checkpoint.transparency_tree_head_sha256
        == transparency_head.tree_head_sha256
    )
    assert checkpoint.previous_checkpoint_sha256 == "0" * 64
    assert checkpoint.checkpoint_id.startswith("qcheckpoint_")


def test_checkpoint_signature_verifies_and_reports_signer_identity() -> None:
    registry, log, _, keyring, signer, descriptor = populated_registry_and_log()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=3),
    )
    signed = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="registry-checkpoint-signature-1",
        issued_at=NOW + timedelta(seconds=3),
        expires_at=NOW + timedelta(minutes=2),
        nonce="registry-checkpoint-nonce-1",
    )

    verified = verify_registry_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=4),
    )

    assert verified.checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert verified.registry_state_root_sha256 == registry.state_root_sha256
    assert verified.signer_key_id == "qualification-key"
    assert verified.signer_owner_id == "qualification-owner"
    assert verified.keyring_generation == 1


def test_checkpoint_chain_proves_monotonic_registry_and_transparency_growth() -> None:
    registry, log, record, keyring, _, _ = populated_registry_and_log()
    first = build_registry_checkpoint(
        registry=registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=3),
    )

    registry.revoke(
        evidence_id=record.evidence_id,
        reason="QUALIFICATION_RETRACTED",
        observed_at=NOW + timedelta(seconds=4),
    )
    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="QUALIFICATION_REVOCATION",
            object_id=f"revocation-{record.evidence_id}",
            object_sha256=registry.head_sha256,
            subject=record.subject,
            subject_version=record.subject_version,
            profile_id=record.profile_id,
            profile_version=record.profile_version,
            published_at=NOW + timedelta(seconds=5),
        ),
        issued_at=NOW + timedelta(seconds=5),
    )
    second = build_registry_checkpoint(
        registry=registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=6),
        previous_checkpoint=first,
    )

    assert second.previous_checkpoint_sha256 == first.checkpoint_sha256
    assert second.registry_event_count > first.registry_event_count
    assert second.transparency_tree_size > first.transparency_tree_size
    assert second.registry_state_root_sha256 != first.registry_state_root_sha256
    assert verify_checkpoint_successor(previous=first, current=second)

    assert keyring.generation == 1


def test_checkpoint_rejects_tampering_and_signature_replay() -> None:
    registry, log, _, keyring, signer, descriptor = populated_registry_and_log()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=3),
    )
    signed = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="registry-checkpoint-signature-replay",
        issued_at=NOW + timedelta(seconds=3),
        expires_at=NOW + timedelta(minutes=2),
        nonce="registry-checkpoint-nonce-replay",
    )
    ledger = QualificationSignatureReplayLedger()

    verify_registry_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=4),
        replay_ledger=ledger,
    )
    assert ledger.size == 1

    with pytest.raises(ValueError, match="replay"):
        verify_registry_checkpoint(
            signed_checkpoint=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=4),
            replay_ledger=ledger,
        )

    tampered = replace(
        signed,
        checkpoint=replace(
            checkpoint,
            registry_state_root_sha256="f" * 64,
        ),
    )
    with pytest.raises(ValueError, match="payload mismatch"):
        tampered.validate()


def test_checkpoint_rejects_time_and_counter_regression() -> None:
    registry, log, _, _, signer, descriptor = populated_registry_and_log()
    first = build_registry_checkpoint(
        registry=registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=5),
    )

    with pytest.raises(ValueError, match="time regression"):
        build_registry_checkpoint(
            registry=registry,
            transparency_head=log.latest_head(),
            issued_at=NOW + timedelta(seconds=4),
            previous_checkpoint=first,
        )

    with pytest.raises(ValueError, match="cannot predate checkpoint"):
        sign_registry_checkpoint(
            checkpoint=first,
            provider=signer,
            descriptor=descriptor,
            keyring_generation=1,
            signature_id="registry-checkpoint-early",
            issued_at=NOW + timedelta(seconds=4),
            expires_at=NOW + timedelta(minutes=1),
            nonce="registry-checkpoint-early",
        )


def test_empty_registry_checkpoint_uses_genesis_state() -> None:
    registry = QualificationEvidenceRegistry()
    log = QualificationTransparencyLog()

    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=log.latest_head(),
        issued_at=NOW,
    )

    assert checkpoint.registry_event_count == 0
    assert checkpoint.registry_event_head_sha256 == "0" * 64
    assert checkpoint.registry_state_root_sha256 == "0" * 64
    assert checkpoint.transparency_tree_size == 0
    assert checkpoint.transparency_root_sha256 == "0" * 64
