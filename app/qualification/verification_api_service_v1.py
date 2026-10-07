from __future__ import annotations

from collections.abc import Mapping

from app.qualification.persistent_trust_state_authority_v1 import (
    PersistentTrustStateAuthorityError,
    PersistentTrustStateAuthorityV1,
    PersistentTrustStateCASMismatch,
    PersistentTrustStateCorruption,
    PersistentTrustStateRecord,
    TrustStateTransitionContext,
)
from app.qualification.portable_artifact_codec import (
    QualificationArtifactCodecError,
    canonical_json_bytes,
    decode_portable_qualification_artifact_json,
)
from app.qualification.portable_bundle_decoder_v4 import (
    QualificationBundleDecodeError,
    decode_typed_portable_qualification_bundle_v4,
)
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIRequestV1,
    VerificationAPIResponseV1,
    VerificationAPIResultClass,
)
from app.qualification.verification_api_idempotency_v1 import (
    VerificationAPIAuthoritySnapshot,
    VerificationAPIIdempotencyConflict,
    VerificationAPIIdempotencyCorruption,
    VerificationAPIIdempotencyError,
    VerificationAPIIdempotencyJournalV1,
    VerificationAPIIdempotencyRecord,
    VerificationAPIIdempotencyState,
)
from app.qualification.verification_api_registry_v1 import (
    VerificationAPIRegistryError,
    VerificationAuthorityRegistryV1,
    VerificationTrustedRootRegistryV1,
)
from app.qualification.verification_service_v4 import (
    QualificationVerificationOutcomeV4,
    QualificationVerificationRequestV4,
    QualificationVerificationServiceResultV4,
    QualificationVerificationServiceV4,
)


class VerificationAPIServiceV1:
    def __init__(
        self,
        *,
        authority_registry: VerificationAuthorityRegistryV1,
        trusted_root_registry: VerificationTrustedRootRegistryV1,
        idempotency_journal: VerificationAPIIdempotencyJournalV1,
    ) -> None:
        self._authorities = authority_registry
        self._root_sets = trusted_root_registry
        self._idempotency = idempotency_journal

    def handle(self, request: VerificationAPIRequestV1) -> dict[str, object]:
        try:
            request.validate()
        except ValueError as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "INVALID_REQUEST",
                str(exc),
            )

        if request.operation is VerificationAPIOperation.AUTHORITY_STATUS:
            return self._authority_status(request)

        try:
            authority = self._authorities.resolve(request.authority_id)
            roots, root_set_sha256 = self._root_sets.resolve_with_digest(
                request.trusted_root_set_id
            )
        except VerificationAPIRegistryError as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "UNKNOWN_CONFIGURATION",
                str(exc),
            )

        if request.operation is VerificationAPIOperation.VERIFY_READ_ONLY:
            return self._verify_read_only(request, authority, roots)
        return self._verify_advance(
            request,
            authority,
            roots,
            root_set_sha256,
        )

    def _authority_status(
        self,
        request: VerificationAPIRequestV1,
    ) -> dict[str, object]:
        try:
            authority = self._authorities.resolve(request.authority_id)
            current = authority.current()
        except VerificationAPIRegistryError as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "UNKNOWN_AUTHORITY",
                str(exc),
            )
        except (PersistentTrustStateAuthorityError, OSError) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "AUTHORITY_UNAVAILABLE",
                str(exc),
            )

        snapshot = _snapshot(current)
        return VerificationAPIResponseV1(
            request_id=request.request_id.strip() or "invalid-request",
            operation=request.operation,
            request_sha256=request.computed_request_sha256,
            result_class=VerificationAPIResultClass.STATUS_OK,
            usable=False,
            authority_generation_before=snapshot.generation,
            authority_record_sha256_before=snapshot.record_sha256,
            authority_trust_state_sha256_before=snapshot.trust_state_sha256,
            authority_generation_after=snapshot.generation,
            authority_record_sha256_after=snapshot.record_sha256,
            authority_trust_state_sha256_after=snapshot.trust_state_sha256,
        ).payload()

    def _verify_read_only(
        self,
        request: VerificationAPIRequestV1,
        authority: PersistentTrustStateAuthorityV1,
        roots: Mapping[str, bytes],
    ) -> dict[str, object]:
        try:
            current = authority.current()
            artifact, bundle = self._decode_for_authority(
                request=request,
                authority_record=current,
            )
            verification = self._verify(
                request=request,
                bundle=bundle,
                artifact_previous_keyring_generation=(
                    artifact.previous_keyring_generation
                ),
                trusted_state=current.trust_state,
                roots=roots,
            )
        except (
            QualificationArtifactCodecError,
            QualificationBundleDecodeError,
            ValueError,
        ) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "INVALID_VERIFICATION_INPUT",
                str(exc),
            )
        except (PersistentTrustStateAuthorityError, OSError) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "AUTHORITY_ERROR",
                str(exc),
            )

        return self._verification_response(
            request=request,
            artifact=artifact,
            verification=verification,
            before=current,
            after=current,
            transition_receipt=None,
        )

    def _verify_advance(
        self,
        request: VerificationAPIRequestV1,
        authority: PersistentTrustStateAuthorityV1,
        roots: Mapping[str, bytes],
        root_set_sha256: str,
    ) -> dict[str, object]:
        if request.idempotency_key is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "MISSING_IDEMPOTENCY_KEY",
                "verify.advance requires idempotency_key",
            )
        request_sha = request.computed_request_sha256

        try:
            existing = self._idempotency.current(
                idempotency_key=request.idempotency_key
            )
        except VerificationAPIIdempotencyCorruption as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_CORRUPTION",
                str(exc),
            )

        if existing is not None:
            if existing.request_sha256 != request_sha:
                return self._error_response(
                    request,
                    VerificationAPIResultClass.IDEMPOTENCY_CONFLICT,
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key is already bound to another request",
                )
            if (
                existing.trusted_root_set_id != request.trusted_root_set_id
                or existing.trusted_root_set_sha256 != root_set_sha256
            ):
                return self._error_response(
                    request,
                    VerificationAPIResultClass.AUTHORITY_ERROR,
                    "TRUST_ROOT_SET_CHANGED",
                    "trusted root set changed since request preparation",
                )
            if existing.state is VerificationAPIIdempotencyState.FINALIZED:
                if existing.response_payload is None:
                    return self._error_response(
                        request,
                        VerificationAPIResultClass.AUTHORITY_ERROR,
                        "IDEMPOTENCY_CORRUPTION",
                        "FINALIZED idempotency record has no response payload",
                    )
                return dict(existing.response_payload)

        try:
            current = authority.current()
            before_record = (
                current
                if existing is None
                else self._record_for_snapshot(
                    authority,
                    existing.authority_before,
                )
            )
            artifact, bundle = self._decode_for_authority(
                request=request,
                authority_record=before_record,
            )
        except (
            QualificationArtifactCodecError,
            QualificationBundleDecodeError,
            ValueError,
        ) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "INVALID_VERIFICATION_INPUT",
                str(exc),
            )
        except (PersistentTrustStateAuthorityError, OSError) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "AUTHORITY_ERROR",
                str(exc),
            )

        checkpoint = bundle.signed_trust_checkpoint_v4.checkpoint
        before_snapshot = _snapshot(before_record)

        if existing is None:
            try:
                existing = self._idempotency.prepare(
                    idempotency_key=request.idempotency_key,
                    request_sha256=request_sha,
                    authority_id=request.authority_id,
                    trusted_root_set_id=request.trusted_root_set_id,
                    trusted_root_set_sha256=root_set_sha256,
                    authority_before=before_snapshot,
                    artifact_id=artifact.artifact_id,
                    artifact_sha256=artifact.artifact_sha256,
                    bundle_id=artifact.bundle_id,
                    bundle_sha256=artifact.bundle_sha256,
                    checkpoint_v4_id=checkpoint.checkpoint_id,
                    checkpoint_v4_sha256=checkpoint.checkpoint_sha256,
                    observed_at=request.observed_at,
                )
            except VerificationAPIIdempotencyConflict as exc:
                return self._error_response(
                    request,
                    VerificationAPIResultClass.IDEMPOTENCY_CONFLICT,
                    "IDEMPOTENCY_CONFLICT",
                    str(exc),
                )
            except VerificationAPIIdempotencyCorruption as exc:
                return self._error_response(
                    request,
                    VerificationAPIResultClass.AUTHORITY_ERROR,
                    "IDEMPOTENCY_CORRUPTION",
                    str(exc),
                )

        if existing.state is VerificationAPIIdempotencyState.AUTHORITY_COMMITTED:
            return self._finalize_committed_recovery(
                request=request,
                authority=authority,
                roots=roots,
                artifact=artifact,
                bundle=bundle,
                record=existing,
            )

        try:
            recovered = self._recover_commit_if_present(
                authority=authority,
                record=existing,
            )
        except (PersistentTrustStateAuthorityError, OSError) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_RECOVERY_ERROR",
                str(exc),
                authority_before=before_snapshot,
            )
        if recovered is not None:
            committed_snapshot, receipt = recovered
            try:
                existing = self._idempotency.mark_authority_committed(
                    idempotency_key=request.idempotency_key,
                    request_sha256=request_sha,
                    authority_after=committed_snapshot,
                    transition_receipt=receipt,
                )
            except VerificationAPIIdempotencyError as exc:
                return self._error_response(
                    request,
                    VerificationAPIResultClass.AUTHORITY_ERROR,
                    "IDEMPOTENCY_RECOVERY_ERROR",
                    str(exc),
                )
            return self._finalize_committed_recovery(
                request=request,
                authority=authority,
                roots=roots,
                artifact=artifact,
                bundle=bundle,
                record=existing,
            )

        verification = self._verify(
            request=request,
            bundle=bundle,
            artifact_previous_keyring_generation=(
                artifact.previous_keyring_generation
            ),
            trusted_state=before_record.trust_state,
            roots=roots,
        )

        if verification.outcome is QualificationVerificationOutcomeV4.REJECTED:
            response = self._verification_response(
                request=request,
                artifact=artifact,
                verification=verification,
                before=before_record,
                after=before_record,
                transition_receipt=None,
            )
            return self._finalize_no_commit(
                request=request,
                response=response,
            )

        if verification.next_trust_state is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "VERIFICATION_INVARIANT_ERROR",
                "VERIFIED result has no next TrustState",
                authority_before=before_snapshot,
            )
        transition = TrustStateTransitionContext(
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.artifact_sha256,
            bundle_id=artifact.bundle_id,
            bundle_sha256=artifact.bundle_sha256,
            checkpoint_v4_id=checkpoint.checkpoint_id,
            checkpoint_v4_sha256=checkpoint.checkpoint_sha256,
            verified_at=request.observed_at,
            operation_context_sha256=request_sha,
        )
        try:
            committed, receipt = authority.advance(
                next_state=verification.next_trust_state,
                transition=transition,
                expected_generation=before_record.generation,
                expected_record_sha256=before_record.record_sha256,
                expected_trust_state_sha256=before_record.trust_state_sha256,
            )
        except PersistentTrustStateCASMismatch as exc:
            response = self._error_response(
                request,
                VerificationAPIResultClass.CAS_CONFLICT,
                "CAS_CONFLICT",
                str(exc),
                authority_before=before_snapshot,
            )
            return self._finalize_no_commit(
                request=request,
                response=response,
            )
        except (PersistentTrustStateAuthorityError, OSError) as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "AUTHORITY_COMMIT_ERROR",
                str(exc),
                authority_before=before_snapshot,
            )

        after_snapshot = _snapshot(committed)
        try:
            self._idempotency.mark_authority_committed(
                idempotency_key=request.idempotency_key,
                request_sha256=request_sha,
                authority_after=after_snapshot,
                transition_receipt=receipt.payload(),
            )
        except VerificationAPIIdempotencyError as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_COMMIT_ERROR",
                str(exc),
                authority_before=before_snapshot,
                authority_after=after_snapshot,
            )

        response = self._verification_response(
            request=request,
            artifact=artifact,
            verification=verification,
            before=before_record,
            after=committed,
            transition_receipt=receipt.payload(),
        )
        return self._finalize_response(request=request, response=response)

    def _finalize_committed_recovery(
        self,
        *,
        request: VerificationAPIRequestV1,
        authority: PersistentTrustStateAuthorityV1,
        roots: Mapping[str, bytes],
        artifact,
        bundle,
        record: VerificationAPIIdempotencyRecord,
    ) -> dict[str, object]:
        if record.authority_after is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_CORRUPTION",
                "AUTHORITY_COMMITTED record has no authority_after snapshot",
                authority_before=record.authority_before,
            )
        before = self._record_for_snapshot(authority, record.authority_before)
        after = self._record_for_snapshot(authority, record.authority_after)
        verification = self._verify(
            request=request,
            bundle=bundle,
            artifact_previous_keyring_generation=(
                artifact.previous_keyring_generation
            ),
            trusted_state=before.trust_state,
            roots=roots,
        )
        if verification.outcome is not QualificationVerificationOutcomeV4.VERIFIED:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "RECOVERY_VERIFICATION_MISMATCH",
                "committed idempotency record no longer verifies from stored snapshot",
                authority_before=record.authority_before,
                authority_after=record.authority_after,
            )
        response = self._verification_response(
            request=request,
            artifact=artifact,
            verification=verification,
            before=before,
            after=after,
            transition_receipt=(
                None
                if record.transition_receipt is None
                else dict(record.transition_receipt)
            ),
        )
        return self._finalize_response(request=request, response=response)

    def _recover_commit_if_present(
        self,
        *,
        authority: PersistentTrustStateAuthorityV1,
        record: VerificationAPIIdempotencyRecord,
    ) -> tuple[VerificationAPIAuthoritySnapshot, dict[str, object]] | None:
        history = authority.export_history()
        target_generation = record.authority_before.generation + 1
        if target_generation >= len(history):
            return None
        candidate = history[target_generation]
        if candidate.previous_record_sha256 != record.authority_before.record_sha256:
            return None
        transition = candidate.transition
        expected = (
            record.artifact_id,
            record.artifact_sha256,
            record.bundle_id,
            record.bundle_sha256,
            record.checkpoint_v4_id,
            record.checkpoint_v4_sha256,
            record.request_sha256,
        )
        actual = (
            transition.artifact_id,
            transition.artifact_sha256,
            transition.bundle_id,
            transition.bundle_sha256,
            transition.checkpoint_v4_id,
            transition.checkpoint_v4_sha256,
            transition.operation_context_sha256,
        )
        if actual != expected:
            return None
        receipt = authority.receipt_for_generation(target_generation)
        return _snapshot(candidate), receipt.payload()

    def _finalize_no_commit(
        self,
        *,
        request: VerificationAPIRequestV1,
        response: dict[str, object],
    ) -> dict[str, object]:
        if request.idempotency_key is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "MISSING_IDEMPOTENCY_KEY",
                "mutating verification requires idempotency_key",
            )
        try:
            finalized = self._idempotency.finalize(
                idempotency_key=request.idempotency_key,
                request_sha256=request.computed_request_sha256,
                response_payload=response,
            )
        except VerificationAPIIdempotencyError as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_FINALIZE_ERROR",
                str(exc),
            )
        if finalized.response_payload is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_CORRUPTION",
                "FINALIZED idempotency record has no response payload",
            )
        return dict(finalized.response_payload)

    def _finalize_response(
        self,
        *,
        request: VerificationAPIRequestV1,
        response: dict[str, object],
    ) -> dict[str, object]:
        if request.idempotency_key is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.INPUT_ERROR,
                "MISSING_IDEMPOTENCY_KEY",
                "mutating verification requires idempotency_key",
            )
        try:
            finalized = self._idempotency.finalize(
                idempotency_key=request.idempotency_key,
                request_sha256=request.computed_request_sha256,
                response_payload=response,
            )
        except VerificationAPIIdempotencyError as exc:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_FINALIZE_ERROR",
                str(exc),
            )
        if finalized.response_payload is None:
            return self._error_response(
                request,
                VerificationAPIResultClass.AUTHORITY_ERROR,
                "IDEMPOTENCY_CORRUPTION",
                "FINALIZED idempotency record has no response payload",
            )
        return dict(finalized.response_payload)

    def _decode_for_authority(
        self,
        *,
        request: VerificationAPIRequestV1,
        authority_record: PersistentTrustStateRecord,
    ):
        artifact = decode_portable_qualification_artifact_json(
            request.artifact_bytes()
        )
        if canonical_json_bytes(artifact.trusted_state.payload()) != canonical_json_bytes(
            authority_record.trust_state.payload()
        ):
            raise ValueError(
                "artifact embedded TrustState does not match authority snapshot"
            )
        bundle = decode_typed_portable_qualification_bundle_v4(artifact)
        return artifact, bundle

    def _verify(
        self,
        *,
        request: VerificationAPIRequestV1,
        bundle,
        artifact_previous_keyring_generation: int,
        trusted_state,
        roots: Mapping[str, bytes],
    ) -> QualificationVerificationServiceResultV4:
        service = QualificationVerificationServiceV4(
            trusted_root_public_keys=roots,
            max_clock_skew_seconds=request.max_clock_skew_seconds,
        )
        return service.verify(
            QualificationVerificationRequestV4(
                bundle=bundle,
                previous_keyring_generation=artifact_previous_keyring_generation,
                trusted_state=trusted_state,
                observed_at=request.observed_at,
            )
        )

    def _verification_response(
        self,
        *,
        request: VerificationAPIRequestV1,
        artifact,
        verification: QualificationVerificationServiceResultV4,
        before: PersistentTrustStateRecord,
        after: PersistentTrustStateRecord,
        transition_receipt: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if verification.outcome is QualificationVerificationOutcomeV4.REJECTED:
            result_class = VerificationAPIResultClass.REJECTED
        elif verification.usable:
            result_class = VerificationAPIResultClass.VERIFIED_USABLE
        else:
            result_class = VerificationAPIResultClass.VERIFIED_UNUSABLE

        return VerificationAPIResponseV1(
            request_id=request.request_id.strip() or "invalid-request",
            operation=request.operation,
            request_sha256=request.computed_request_sha256,
            result_class=result_class,
            usable=verification.usable,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.artifact_sha256,
            bundle_id=verification.bundle_id or artifact.bundle_id,
            bundle_sha256=verification.bundle_sha256 or artifact.bundle_sha256,
            authority_generation_before=before.generation,
            authority_record_sha256_before=before.record_sha256,
            authority_trust_state_sha256_before=before.trust_state_sha256,
            authority_generation_after=after.generation,
            authority_record_sha256_after=after.record_sha256,
            authority_trust_state_sha256_after=after.trust_state_sha256,
            transition_receipt=(
                None if transition_receipt is None else dict(transition_receipt)
            ),
            failure_code=(
                None
                if verification.failure_code is None
                else verification.failure_code.value
            ),
            failure_detail=verification.failure_detail,
        ).payload()

    def _record_for_snapshot(
        self,
        authority: PersistentTrustStateAuthorityV1,
        snapshot: VerificationAPIAuthoritySnapshot,
    ) -> PersistentTrustStateRecord:
        history = authority.export_history()
        if snapshot.generation >= len(history):
            raise PersistentTrustStateCorruption(
                "authority snapshot generation is not retained"
            )
        record = history[snapshot.generation]
        if (
            record.record_sha256 != snapshot.record_sha256
            or record.trust_state_sha256 != snapshot.trust_state_sha256
        ):
            raise PersistentTrustStateCorruption(
                "authority snapshot does not match retained history"
            )
        return record

    def _error_response(
        self,
        request: VerificationAPIRequestV1,
        result_class: VerificationAPIResultClass,
        failure_code: str,
        failure_detail: str,
        *,
        authority_before: VerificationAPIAuthoritySnapshot | None = None,
        authority_after: VerificationAPIAuthoritySnapshot | None = None,
    ) -> dict[str, object]:
        return VerificationAPIResponseV1(
            request_id=request.request_id,
            operation=request.operation,
            request_sha256=request.computed_request_sha256,
            result_class=result_class,
            usable=False,
            authority_generation_before=(
                None if authority_before is None else authority_before.generation
            ),
            authority_record_sha256_before=(
                None if authority_before is None else authority_before.record_sha256
            ),
            authority_trust_state_sha256_before=(
                None
                if authority_before is None
                else authority_before.trust_state_sha256
            ),
            authority_generation_after=(
                None if authority_after is None else authority_after.generation
            ),
            authority_record_sha256_after=(
                None if authority_after is None else authority_after.record_sha256
            ),
            authority_trust_state_sha256_after=(
                None
                if authority_after is None
                else authority_after.trust_state_sha256
            ),
            failure_code=failure_code,
            failure_detail=failure_detail,
        ).payload()


def _snapshot(record: PersistentTrustStateRecord) -> VerificationAPIAuthoritySnapshot:
    return VerificationAPIAuthoritySnapshot(
        generation=record.generation,
        record_sha256=record.record_sha256,
        trust_state_sha256=record.trust_state_sha256,
    )
