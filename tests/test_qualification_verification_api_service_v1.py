from __future__ import annotations

from datetime import timedelta

from app.qualification.persistent_trust_state_authority_v1 import (
    PersistentTrustStateAuthorityV1,
    TrustStateTransitionContext,
)
from app.qualification.portable_artifact_codec import (
    canonical_json_bytes,
    decode_portable_qualification_artifact_json,
    encode_portable_qualification_artifact_json,
)
from app.qualification.portable_bundle_decoder_v4 import (
    decode_typed_portable_qualification_bundle_v4,
)
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIRequestV1,
    VerificationAPIResultClass,
    encode_artifact_b64,
)
from app.qualification.verification_api_idempotency_v1 import (
    VerificationAPIAuthoritySnapshot,
    VerificationAPIIdempotencyJournalV1,
    VerificationAPIIdempotencyState,
)
from app.qualification.verification_api_registry_v1 import (
    VerificationAuthorityRegistryV1,
    VerificationTrustedRootRegistryV1,
)
from app.qualification.verification_api_service_v1 import VerificationAPIServiceV1
from app.qualification.verification_service_v4 import (
    QualificationTrustStateV4,
    QualificationVerificationRequestV4,
    QualificationVerificationServiceV4,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification_v4 import bundle_v4


def _setup(tmp_path):
    bundle, root = bundle_v4()
    state = QualificationTrustStateV4(
        profile_event_count=0,
        profile_event_head_sha256="0" * 64,
        transparency_tree_size=bundle.base_v3.transparency_head.tree_size,
        transparency_root_sha256=bundle.base_v3.transparency_head.root_sha256,
        checkpoint_v4_sha256="0" * 64,
    )
    artifact_bytes = encode_portable_qualification_artifact_json(
        bundle=bundle,
        trusted_state=state,
        previous_keyring_generation=0,
    )
    authority_dir = (tmp_path / "authority").resolve()
    authority = PersistentTrustStateAuthorityV1(authority_dir)
    authority.initialize(
        initial_state=state,
        transition=TrustStateTransitionContext(
            artifact_id="bootstrap-artifact",
            artifact_sha256="0" * 64,
            bundle_id="bootstrap-bundle",
            bundle_sha256="0" * 64,
            checkpoint_v4_id="qtrustv4_genesis",
            checkpoint_v4_sha256="0" * 64,
            verified_at=NOW,
        ),
    )
    roots = {root.key_id: root.public_key_bytes()}
    journal = VerificationAPIIdempotencyJournalV1(
        (tmp_path / "idempotency").resolve()
    )
    service = VerificationAPIServiceV1(
        authority_registry=VerificationAuthorityRegistryV1(
            {"primary": authority_dir}
        ),
        trusted_root_registry=VerificationTrustedRootRegistryV1(
            {
                "good-roots": roots,
                "wrong-roots": {"wrong-root": b"x" * 32},
            }
        ),
        idempotency_journal=journal,
    )
    return service, authority, journal, artifact_bytes, roots


def _request(
    artifact_bytes: bytes,
    *,
    operation: VerificationAPIOperation,
    request_id: str,
    idempotency_key: str | None = None,
    root_set_id: str = "good-roots",
) -> VerificationAPIRequestV1:
    return VerificationAPIRequestV1(
        operation=operation,
        request_id=request_id,
        authority_id="primary",
        trusted_root_set_id=root_set_id,
        artifact_b64=(
            None
            if operation is VerificationAPIOperation.AUTHORITY_STATUS
            else encode_artifact_b64(artifact_bytes)
        ),
        idempotency_key=idempotency_key,
        observed_at=NOW + timedelta(seconds=11),
    )


def test_authority_status_is_not_a_verification_result(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.AUTHORITY_STATUS,
        request_id="status-1",
    )

    response = service.handle(request)

    assert response["result_class"] == VerificationAPIResultClass.STATUS_OK.value
    assert response["usable"] is False
    assert response["authority_generation_before"] == before.generation
    assert response["authority_generation_after"] == before.generation
    assert authority.current() == before


def test_read_only_verification_is_authority_immutable(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_id="read-1",
    )

    response = service.handle(request)

    assert response["result_class"] == (
        VerificationAPIResultClass.VERIFIED_USABLE.value
    )
    assert response["usable"] is True
    assert response["transition_receipt"] is None
    assert authority.current() == before


def test_advance_commits_once_and_replays_byte_identically(tmp_path) -> None:
    service, authority, journal, artifact_bytes, _ = _setup(tmp_path)
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="advance-1",
        idempotency_key="idem-advance-1",
    )

    first = service.handle(request)
    after_first = authority.current()
    second = service.handle(request)
    after_second = authority.current()

    assert first["result_class"] == (
        VerificationAPIResultClass.VERIFIED_USABLE.value
    )
    assert first["transition_receipt"] is not None
    assert after_first.generation == 1
    assert after_second == after_first
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    stored = journal.current(idempotency_key="idem-advance-1")
    assert stored is not None
    assert stored.state is VerificationAPIIdempotencyState.FINALIZED


def test_same_idempotency_key_with_different_request_fails_closed(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    first_request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="advance-1",
        idempotency_key="shared-key",
    )
    second_request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="advance-2",
        idempotency_key="shared-key",
    )

    first = service.handle(first_request)
    after_first = authority.current()
    second = service.handle(second_request)

    assert first["result_class"] == (
        VerificationAPIResultClass.VERIFIED_USABLE.value
    )
    assert second["result_class"] == (
        VerificationAPIResultClass.IDEMPOTENCY_CONFLICT.value
    )
    assert authority.current() == after_first


def test_rejected_advance_is_finalized_without_authority_mutation(tmp_path) -> None:
    service, authority, journal, artifact_bytes, roots = _setup(tmp_path)
    _, root_set_sha256 = VerificationTrustedRootRegistryV1(
        {"good-roots": roots}
    ).resolve_with_digest("good-roots")
    before = authority.current()
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="reject-1",
        idempotency_key="idem-reject-1",
        root_set_id="wrong-roots",
    )

    first = service.handle(request)
    second = service.handle(request)

    assert first["result_class"] == VerificationAPIResultClass.REJECTED.value
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert authority.current() == before
    stored = journal.current(idempotency_key="idem-reject-1")
    assert stored is not None
    assert stored.state is VerificationAPIIdempotencyState.FINALIZED
    assert stored.transition_receipt is None


def test_prepared_competing_request_becomes_cas_conflict_not_false_recovery(
    tmp_path,
) -> None:
    service, authority, journal, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    artifact = decode_portable_qualification_artifact_json(artifact_bytes)
    bundle = decode_typed_portable_qualification_bundle_v4(artifact)
    checkpoint = bundle.signed_trust_checkpoint_v4.checkpoint
    request_a = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="advance-a",
        idempotency_key="idem-a",
    )
    request_b = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="advance-b",
        idempotency_key="idem-b",
    )
    journal.prepare(
        idempotency_key="idem-b",
        request_sha256=request_b.computed_request_sha256,
        authority_id="primary",
        trusted_root_set_id="good-roots",
        trusted_root_set_sha256=root_set_sha256,
        authority_before=VerificationAPIAuthoritySnapshot(
            generation=before.generation,
            record_sha256=before.record_sha256,
            trust_state_sha256=before.trust_state_sha256,
        ),
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.artifact_sha256,
        bundle_id=artifact.bundle_id,
        bundle_sha256=artifact.bundle_sha256,
        checkpoint_v4_id=checkpoint.checkpoint_id,
        checkpoint_v4_sha256=checkpoint.checkpoint_sha256,
        observed_at=request_b.observed_at,
    )

    first = service.handle(request_a)
    second = service.handle(request_b)

    assert first["result_class"] == (
        VerificationAPIResultClass.VERIFIED_USABLE.value
    )
    assert second["result_class"] == VerificationAPIResultClass.CAS_CONFLICT.value
    assert authority.current().generation == 1


def test_crash_after_authority_commit_recovers_without_second_commit(tmp_path) -> None:
    service, authority, journal, artifact_bytes, roots = _setup(tmp_path)
    _, root_set_sha256 = VerificationTrustedRootRegistryV1(
        {"good-roots": roots}
    ).resolve_with_digest("good-roots")
    before = authority.current()
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="crash-1",
        idempotency_key="idem-crash-1",
    )
    artifact = decode_portable_qualification_artifact_json(artifact_bytes)
    bundle = decode_typed_portable_qualification_bundle_v4(artifact)
    checkpoint = bundle.signed_trust_checkpoint_v4.checkpoint
    journal.prepare(
        idempotency_key="idem-crash-1",
        request_sha256=request.computed_request_sha256,
        authority_id="primary",
        trusted_root_set_id="good-roots",
        trusted_root_set_sha256=root_set_sha256,
        authority_before=VerificationAPIAuthoritySnapshot(
            generation=before.generation,
            record_sha256=before.record_sha256,
            trust_state_sha256=before.trust_state_sha256,
        ),
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.artifact_sha256,
        bundle_id=artifact.bundle_id,
        bundle_sha256=artifact.bundle_sha256,
        checkpoint_v4_id=checkpoint.checkpoint_id,
        checkpoint_v4_sha256=checkpoint.checkpoint_sha256,
        observed_at=request.observed_at,
    )
    verification = QualificationVerificationServiceV4(
        trusted_root_public_keys=roots,
    ).verify(
        QualificationVerificationRequestV4(
            bundle=bundle,
            previous_keyring_generation=artifact.previous_keyring_generation,
            trusted_state=before.trust_state,
            observed_at=request.observed_at,
        )
    )
    assert verification.next_trust_state is not None
    authority.advance(
        next_state=verification.next_trust_state,
        transition=TrustStateTransitionContext(
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.artifact_sha256,
            bundle_id=artifact.bundle_id,
            bundle_sha256=artifact.bundle_sha256,
            checkpoint_v4_id=checkpoint.checkpoint_id,
            checkpoint_v4_sha256=checkpoint.checkpoint_sha256,
            verified_at=request.observed_at,
            operation_context_sha256=request.computed_request_sha256,
        ),
        expected_generation=before.generation,
        expected_record_sha256=before.record_sha256,
        expected_trust_state_sha256=before.trust_state_sha256,
    )

    response = service.handle(request)

    assert response["result_class"] == (
        VerificationAPIResultClass.VERIFIED_USABLE.value
    )
    assert response["transition_receipt"] is not None
    assert authority.current().generation == 1
    stored = journal.current(idempotency_key="idem-crash-1")
    assert stored is not None
    assert stored.state is VerificationAPIIdempotencyState.FINALIZED


def test_prepared_request_fails_closed_when_root_set_content_changes(
    tmp_path,
) -> None:
    service, authority, journal, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    artifact = decode_portable_qualification_artifact_json(artifact_bytes)
    bundle = decode_typed_portable_qualification_bundle_v4(artifact)
    checkpoint = bundle.signed_trust_checkpoint_v4.checkpoint
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="root-drift-1",
        idempotency_key="idem-root-drift-1",
    )
    journal.prepare(
        idempotency_key="idem-root-drift-1",
        request_sha256=request.computed_request_sha256,
        authority_id="primary",
        trusted_root_set_id="good-roots",
        trusted_root_set_sha256="f" * 64,
        authority_before=VerificationAPIAuthoritySnapshot(
            generation=before.generation,
            record_sha256=before.record_sha256,
            trust_state_sha256=before.trust_state_sha256,
        ),
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.artifact_sha256,
        bundle_id=artifact.bundle_id,
        bundle_sha256=artifact.bundle_sha256,
        checkpoint_v4_id=checkpoint.checkpoint_id,
        checkpoint_v4_sha256=checkpoint.checkpoint_sha256,
        observed_at=request.observed_at,
    )

    response = service.handle(request)

    assert response["result_class"] == VerificationAPIResultClass.AUTHORITY_ERROR.value
    assert response["failure_code"] == "TRUST_ROOT_SET_CHANGED"
    assert authority.current() == before
