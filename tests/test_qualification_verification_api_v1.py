from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIRequestV1,
    VerificationAPIResponseV1,
    VerificationAPIResultClass,
    encode_artifact_b64,
)
from app.qualification.verification_api_idempotency_v1 import (
    VerificationAPIAuthoritySnapshot,
    VerificationAPIIdempotencyConflict,
    VerificationAPIIdempotencyJournalV1,
    VerificationAPIIdempotencyState,
)
from app.qualification.verification_api_registry_v1 import (
    VerificationAPIRegistryError,
    VerificationAuthorityRegistryV1,
    VerificationTrustedRootRegistryV1,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _snapshot(
    generation: int = 0,
    *,
    record: str = "1",
    state: str = "2",
) -> VerificationAPIAuthoritySnapshot:
    return VerificationAPIAuthoritySnapshot(
        generation=generation,
        record_sha256=record * 64,
        trust_state_sha256=state * 64,
    )


def test_request_digest_is_deterministic() -> None:
    request = VerificationAPIRequestV1(
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="req-1",
        authority_id="qualification-primary",
        trusted_root_set_id="institutional-roots-v1",
        artifact_b64=encode_artifact_b64(b'{"artifact":"canonical"}'),
        idempotency_key="idem-1",
        observed_at=NOW,
    )

    first = request.payload()
    second = request.payload()

    assert first == second
    assert first["request_sha256"] == request.computed_request_sha256


def test_request_rejects_wrong_embedded_digest() -> None:
    request = VerificationAPIRequestV1(
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_id="req-1",
        authority_id="qualification-primary",
        trusted_root_set_id="institutional-roots-v1",
        artifact_b64=encode_artifact_b64(b"artifact"),
        observed_at=NOW,
        request_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="request digest mismatch"):
        request.validate()


def test_operation_specific_request_rules_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires idempotency_key"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_ADVANCE,
            request_id="req-1",
            authority_id="qualification-primary",
            trusted_root_set_id="roots",
            artifact_b64=encode_artifact_b64(b"artifact"),
            observed_at=NOW,
        ).validate()

    with pytest.raises(ValueError, match="must not include idempotency_key"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_id="req-2",
            authority_id="qualification-primary",
            trusted_root_set_id="roots",
            artifact_b64=encode_artifact_b64(b"artifact"),
            idempotency_key="not-allowed",
            observed_at=NOW,
        ).validate()

    with pytest.raises(ValueError, match="must not include an artifact"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_id="req-3",
            authority_id="qualification-primary",
            trusted_root_set_id="roots",
            artifact_b64=encode_artifact_b64(b"artifact"),
            observed_at=NOW,
        ).validate()


def test_response_digest_is_deterministic_and_semantic() -> None:
    response = VerificationAPIResponseV1(
        request_id="req-1",
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_sha256="a" * 64,
        result_class=VerificationAPIResultClass.VERIFIED_USABLE,
        usable=True,
        artifact_id="qartifact_abc",
        artifact_sha256="b" * 64,
        bundle_id="qverifyv4_abc",
        bundle_sha256="c" * 64,
        authority_generation_before=1,
        authority_record_sha256_before="d" * 64,
        authority_trust_state_sha256_before="e" * 64,
        authority_generation_after=1,
        authority_record_sha256_after="d" * 64,
        authority_trust_state_sha256_after="e" * 64,
    )

    first = response.payload()
    second = response.payload()

    assert first == second
    assert first["response_sha256"] == response.computed_response_sha256

    with pytest.raises(ValueError, match="only VERIFIED_USABLE"):
        VerificationAPIResponseV1(
            request_id="req-2",
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_sha256="a" * 64,
            result_class=VerificationAPIResultClass.REJECTED,
            usable=True,
        ).validate()


def test_authority_registry_resolves_only_configured_absolute_paths(tmp_path) -> None:
    registry = VerificationAuthorityRegistryV1(
        {"primary": tmp_path.resolve() / "authority"}
    )

    resolved = registry.resolve("primary")

    assert resolved is not None
    with pytest.raises(VerificationAPIRegistryError, match="unknown authority_id"):
        registry.resolve("missing")
    with pytest.raises(VerificationAPIRegistryError, match="must be absolute"):
        VerificationAuthorityRegistryV1({"bad": tmp_path.relative_to(tmp_path)})


def test_trusted_root_registry_validates_ed25519_root_sets() -> None:
    registry = VerificationTrustedRootRegistryV1(
        {"roots-v1": {"root-1": b"x" * 32}}
    )

    assert registry.resolve("roots-v1")["root-1"] == b"x" * 32
    with pytest.raises(VerificationAPIRegistryError, match="unknown"):
        registry.resolve("missing")
    with pytest.raises(VerificationAPIRegistryError, match="must be 32 bytes"):
        VerificationTrustedRootRegistryV1(
            {"bad": {"root-1": b"x" * 31}}
        )


def test_idempotency_prepare_is_replayable_and_digest_bound(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    prepared = journal.prepare(
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        authority_id="primary",
        authority_before=_snapshot(),
        artifact_id="qartifact_a",
        artifact_sha256="b" * 64,
        bundle_id="qverifyv4_a",
        bundle_sha256="c" * 64,
        checkpoint_v4_id="qtrustv4_a",
        checkpoint_v4_sha256="d" * 64,
        observed_at=NOW,
    )
    replay = journal.prepare(
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        authority_id="primary",
        authority_before=_snapshot(),
        artifact_id="qartifact_a",
        artifact_sha256="b" * 64,
        bundle_id="qverifyv4_a",
        bundle_sha256="c" * 64,
        checkpoint_v4_id="qtrustv4_a",
        checkpoint_v4_sha256="d" * 64,
        observed_at=NOW,
    )

    assert replay == prepared
    assert prepared.state is VerificationAPIIdempotencyState.PREPARED

    with pytest.raises(VerificationAPIIdempotencyConflict, match="different request"):
        journal.prepare(
            idempotency_key="idem-1",
            request_sha256="f" * 64,
            authority_id="primary",
            authority_before=_snapshot(),
            artifact_id="qartifact_a",
            artifact_sha256="b" * 64,
            bundle_id="qverifyv4_a",
            bundle_sha256="c" * 64,
            checkpoint_v4_id="qtrustv4_a",
            checkpoint_v4_sha256="d" * 64,
            observed_at=NOW,
        )


def test_idempotency_commit_finalize_and_exact_replay(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    prepared = journal.prepare(
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        authority_id="primary",
        authority_before=_snapshot(),
        artifact_id="qartifact_a",
        artifact_sha256="b" * 64,
        bundle_id="qverifyv4_a",
        bundle_sha256="c" * 64,
        checkpoint_v4_id="qtrustv4_a",
        checkpoint_v4_sha256="d" * 64,
        observed_at=NOW,
    )
    committed = journal.mark_authority_committed(
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        authority_after=_snapshot(1, record="3", state="4"),
        transition_receipt={"receipt_sha256": "5" * 64},
    )
    response = {
        "schema_version": "astra-verification-api-response-v1",
        "request_id": "req-1",
        "response_sha256": "6" * 64,
    }
    finalized = journal.finalize(
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        response_payload=response,
    )

    assert committed.generation == prepared.generation + 1
    assert committed.state is VerificationAPIIdempotencyState.AUTHORITY_COMMITTED
    assert finalized.state is VerificationAPIIdempotencyState.FINALIZED
    assert finalized.generation == committed.generation + 1
    assert journal.current(idempotency_key="idem-1") == finalized
    assert (
        journal.finalize(
            idempotency_key="idem-1",
            request_sha256="a" * 64,
            response_payload=response,
        )
        == finalized
    )


def test_idempotency_key_is_hashed_for_storage_path(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    secret_key = "../../operator-secret"
    journal.prepare(
        idempotency_key=secret_key,
        request_sha256="a" * 64,
        authority_id="primary",
        authority_before=_snapshot(),
        artifact_id=None,
        artifact_sha256=None,
        bundle_id=None,
        bundle_sha256=None,
        checkpoint_v4_id=None,
        checkpoint_v4_sha256=None,
        observed_at=NOW,
    )

    children = [path.name for path in tmp_path.iterdir()]

    assert secret_key not in children
    assert len(children) == 1
    assert len(children[0]) == 64
