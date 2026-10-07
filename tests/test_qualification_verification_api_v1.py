from __future__ import annotations

from datetime import UTC, datetime

import pytest

import app.qualification.verification_api_idempotency_v1 as idempotency_module
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
    VerificationAPIIdempotencyCorruption,
    VerificationAPIIdempotencyError,
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

    roots, digest = registry.resolve_with_digest("roots-v1")
    assert roots["root-1"] == b"x" * 32
    assert len(digest) == 64
    assert registry.resolve_with_digest("roots-v1")[1] == digest
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
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
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
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
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
            trusted_root_set_id="roots-v1",
            trusted_root_set_sha256="e" * 64,
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
        trusted_root_set_id="good-roots",
        trusted_root_set_sha256="e" * 64,
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
        trusted_root_set_id="good-roots",
        trusted_root_set_sha256="e" * 64,
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


def test_idempotency_recovers_crash_after_commit_history_before_current(
    tmp_path,
    monkeypatch,
) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    journal.prepare(
        idempotency_key="idem-crash-commit",
        request_sha256="a" * 64,
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
        authority_before=_snapshot(),
        artifact_id="qartifact_a",
        artifact_sha256="b" * 64,
        bundle_id="qverifyv4_a",
        bundle_sha256="c" * 64,
        checkpoint_v4_id="qtrustv4_a",
        checkpoint_v4_sha256="d" * 64,
        observed_at=NOW,
    )
    original_write_current = journal._write_current

    def crash_before_current(*args, **kwargs) -> None:
        raise OSError("simulated crash before current pointer")

    monkeypatch.setattr(journal, "_write_current", crash_before_current)
    with pytest.raises(OSError, match="simulated crash"):
        journal.mark_authority_committed(
            idempotency_key="idem-crash-commit",
            request_sha256="a" * 64,
            authority_after=_snapshot(1, record="3", state="4"),
            transition_receipt={"receipt_sha256": "5" * 64},
        )

    monkeypatch.setattr(journal, "_write_current", original_write_current)
    recovered = journal.current(idempotency_key="idem-crash-commit")

    assert recovered is not None
    assert recovered.state is VerificationAPIIdempotencyState.AUTHORITY_COMMITTED
    assert recovered.generation == 1
    replay = journal.mark_authority_committed(
        idempotency_key="idem-crash-commit",
        request_sha256="a" * 64,
        authority_after=_snapshot(1, record="3", state="4"),
        transition_receipt={"receipt_sha256": "5" * 64},
    )
    assert replay == recovered


def test_idempotency_recovers_crash_after_finalize_history_before_current(
    tmp_path,
    monkeypatch,
) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    journal.prepare(
        idempotency_key="idem-crash-finalize",
        request_sha256="a" * 64,
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
        authority_before=_snapshot(),
        artifact_id="qartifact_a",
        artifact_sha256="b" * 64,
        bundle_id="qverifyv4_a",
        bundle_sha256="c" * 64,
        checkpoint_v4_id="qtrustv4_a",
        checkpoint_v4_sha256="d" * 64,
        observed_at=NOW,
    )
    journal.mark_authority_committed(
        idempotency_key="idem-crash-finalize",
        request_sha256="a" * 64,
        authority_after=_snapshot(1, record="3", state="4"),
        transition_receipt={"receipt_sha256": "5" * 64},
    )
    response = {"result_class": "VERIFIED_USABLE"}
    original_write_current = journal._write_current

    def crash_before_current(*args, **kwargs) -> None:
        raise OSError("simulated crash before final current pointer")

    monkeypatch.setattr(journal, "_write_current", crash_before_current)
    with pytest.raises(OSError, match="simulated crash"):
        journal.finalize(
            idempotency_key="idem-crash-finalize",
            request_sha256="a" * 64,
            response_payload=response,
        )

    monkeypatch.setattr(journal, "_write_current", original_write_current)
    recovered = journal.current(idempotency_key="idem-crash-finalize")

    assert recovered is not None
    assert recovered.state is VerificationAPIIdempotencyState.FINALIZED
    assert recovered.generation == 2
    assert recovered.response_payload == response


def test_idempotency_history_gap_or_duplicate_fails_closed(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    prepared = journal.prepare(
        idempotency_key="idem-corrupt-chain",
        request_sha256="a" * 64,
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
        authority_before=_snapshot(),
        artifact_id=None,
        artifact_sha256=None,
        bundle_id=None,
        bundle_sha256=None,
        checkpoint_v4_id=None,
        checkpoint_v4_sha256=None,
        observed_at=NOW,
    )
    key_dir = next(tmp_path.iterdir())
    duplicate_path = key_dir / "history" / "00000000000000000001.json"
    source_path = key_dir / "history" / "00000000000000000000.json"
    duplicate_path.write_bytes(source_path.read_bytes())

    with pytest.raises(
        VerificationAPIIdempotencyCorruption,
        match="generation gap or duplicate",
    ):
        journal.current(idempotency_key="idem-corrupt-chain")

    assert prepared.generation == 0


@pytest.mark.parametrize(
    ("request", "message"),
    [
        (
            VerificationAPIRequestV1(
                operation=VerificationAPIOperation.VERIFY_READ_ONLY,
                request_id="",
                authority_id="authority",
                trusted_root_set_id="roots",
                artifact_b64=encode_artifact_b64(b"artifact"),
                observed_at=NOW,
            ),
            "request_id is required",
        ),
        (
            VerificationAPIRequestV1(
                operation=VerificationAPIOperation.VERIFY_READ_ONLY,
                request_id="req",
                authority_id="",
                trusted_root_set_id="roots",
                artifact_b64=encode_artifact_b64(b"artifact"),
                observed_at=NOW,
            ),
            "authority_id is required",
        ),
        (
            VerificationAPIRequestV1(
                operation=VerificationAPIOperation.VERIFY_READ_ONLY,
                request_id="req",
                authority_id="authority",
                trusted_root_set_id="",
                artifact_b64=encode_artifact_b64(b"artifact"),
                observed_at=NOW,
            ),
            "trusted_root_set_id is required",
        ),
        (
            VerificationAPIRequestV1(
                operation=VerificationAPIOperation.VERIFY_READ_ONLY,
                request_id="req",
                authority_id="authority",
                trusted_root_set_id="roots",
                artifact_b64=encode_artifact_b64(b"artifact"),
                observed_at=datetime(2026, 10, 7, 12, 0),
            ),
            "observed_at must be timezone-aware",
        ),
        (
            VerificationAPIRequestV1(
                operation=VerificationAPIOperation.VERIFY_READ_ONLY,
                request_id="req",
                authority_id="authority",
                trusted_root_set_id="roots",
                artifact_b64=encode_artifact_b64(b"artifact"),
                observed_at=NOW,
                max_clock_skew_seconds=-1,
            ),
            "max_clock_skew_seconds must be non-negative",
        ),
    ],
)
def test_request_validation_rejects_invalid_contract(
    request: VerificationAPIRequestV1,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        request.validate()


def test_request_rejects_schema_missing_artifact_and_invalid_base64() -> None:
    with pytest.raises(ValueError, match="schema mismatch"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_id="req",
            authority_id="authority",
            trusted_root_set_id="roots",
            observed_at=NOW,
            schema_version="wrong",
        ).validate()

    with pytest.raises(ValueError, match="require artifact_b64"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_id="req",
            authority_id="authority",
            trusted_root_set_id="roots",
            observed_at=NOW,
        ).validate()

    with pytest.raises(ValueError, match="strict base64"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_id="req",
            authority_id="authority",
            trusted_root_set_id="roots",
            artifact_b64="not base64!",
            observed_at=NOW,
        ).validate()

    with pytest.raises(ValueError, match="empty bytes"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_id="req",
            authority_id="authority",
            trusted_root_set_id="roots",
            artifact_b64="",
            observed_at=NOW,
        ).validate()


def test_authority_status_rejects_idempotency_key() -> None:
    with pytest.raises(ValueError, match="must not include an idempotency key"):
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_id="req",
            authority_id="authority",
            trusted_root_set_id="roots",
            idempotency_key="idem",
            observed_at=NOW,
        ).validate()


def test_response_validation_rejects_schema_digest_and_generation_errors() -> None:
    with pytest.raises(ValueError, match="response schema mismatch"):
        VerificationAPIResponseV1(
            request_id="req",
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_sha256="a" * 64,
            result_class=VerificationAPIResultClass.STATUS_OK,
            usable=False,
            schema_version="wrong",
        ).validate()

    with pytest.raises(ValueError, match="request_id is required"):
        VerificationAPIResponseV1(
            request_id="",
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_sha256="a" * 64,
            result_class=VerificationAPIResultClass.STATUS_OK,
            usable=False,
        ).validate()

    with pytest.raises(ValueError, match="must be a sha256 digest"):
        VerificationAPIResponseV1(
            request_id="req",
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_sha256="bad",
            result_class=VerificationAPIResultClass.STATUS_OK,
            usable=False,
        ).validate()

    with pytest.raises(ValueError, match="must be non-negative"):
        VerificationAPIResponseV1(
            request_id="req",
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_sha256="a" * 64,
            result_class=VerificationAPIResultClass.STATUS_OK,
            usable=False,
            authority_generation_before=-1,
        ).validate()

    with pytest.raises(ValueError, match="requires usable=true"):
        VerificationAPIResponseV1(
            request_id="req",
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_sha256="a" * 64,
            result_class=VerificationAPIResultClass.VERIFIED_USABLE,
            usable=False,
        ).validate()


def test_response_rejects_wrong_embedded_digest() -> None:
    response = VerificationAPIResponseV1(
        request_id="req",
        operation=VerificationAPIOperation.AUTHORITY_STATUS,
        request_sha256="a" * 64,
        result_class=VerificationAPIResultClass.STATUS_OK,
        usable=False,
        response_sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="response digest mismatch"):
        response.validate()


def test_registry_rejects_blank_ids_empty_sets_and_blank_key_ids(tmp_path) -> None:
    with pytest.raises(VerificationAPIRegistryError, match="cannot be blank"):
        VerificationAuthorityRegistryV1({" ": (tmp_path / "a").resolve()})

    registry = VerificationAuthorityRegistryV1(
        {"primary": (tmp_path / "a").resolve()}
    )
    with pytest.raises(VerificationAPIRegistryError, match="cannot be blank"):
        registry.resolve(" ")

    with pytest.raises(VerificationAPIRegistryError, match="cannot be blank"):
        VerificationTrustedRootRegistryV1({" ": {"root": b"x" * 32}})

    with pytest.raises(VerificationAPIRegistryError, match="cannot be empty"):
        VerificationTrustedRootRegistryV1({"roots": {}})

    with pytest.raises(VerificationAPIRegistryError, match="blank trusted root key_id"):
        VerificationTrustedRootRegistryV1({"roots": {" ": b"x" * 32}})

    roots = VerificationTrustedRootRegistryV1(
        {"roots": {"root": b"x" * 32}}
    )
    with pytest.raises(VerificationAPIRegistryError, match="cannot be blank"):
        roots.resolve(" ")


def test_authority_snapshot_validation_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="generation must be non-negative"):
        VerificationAPIAuthoritySnapshot(
            generation=-1,
            record_sha256="1" * 64,
            trust_state_sha256="2" * 64,
        ).validate()
    with pytest.raises(ValueError, match="must be a sha256 digest"):
        VerificationAPIAuthoritySnapshot(
            generation=0,
            record_sha256="bad",
            trust_state_sha256="2" * 64,
        ).validate()


def test_idempotency_requires_prepare_and_matching_request(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)

    with pytest.raises(VerificationAPIIdempotencyError, match="not prepared"):
        journal.mark_authority_committed(
            idempotency_key="missing",
            request_sha256="a" * 64,
            authority_after=_snapshot(1, record="3", state="4"),
            transition_receipt={"receipt_sha256": "5" * 64},
        )

    journal.prepare(
        idempotency_key="idem",
        request_sha256="a" * 64,
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
        authority_before=_snapshot(),
        artifact_id=None,
        artifact_sha256=None,
        bundle_id=None,
        bundle_sha256=None,
        checkpoint_v4_id=None,
        checkpoint_v4_sha256=None,
        observed_at=NOW,
    )
    with pytest.raises(VerificationAPIIdempotencyConflict, match="different request"):
        journal.finalize(
            idempotency_key="idem",
            request_sha256="f" * 64,
            response_payload={"result": "no"},
        )


def test_idempotency_committed_replay_mismatch_fails_closed(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    journal.prepare(
        idempotency_key="idem",
        request_sha256="a" * 64,
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
        authority_before=_snapshot(),
        artifact_id=None,
        artifact_sha256=None,
        bundle_id=None,
        bundle_sha256=None,
        checkpoint_v4_id=None,
        checkpoint_v4_sha256=None,
        observed_at=NOW,
    )
    journal.mark_authority_committed(
        idempotency_key="idem",
        request_sha256="a" * 64,
        authority_after=_snapshot(1, record="3", state="4"),
        transition_receipt={"receipt_sha256": "5" * 64},
    )

    with pytest.raises(
        VerificationAPIIdempotencyCorruption,
        match="does not match stored state",
    ):
        journal.mark_authority_committed(
            idempotency_key="idem",
            request_sha256="a" * 64,
            authority_after=_snapshot(2, record="6", state="7"),
            transition_receipt={"receipt_sha256": "8" * 64},
        )


def test_idempotency_finalized_response_mismatch_fails_closed(tmp_path) -> None:
    journal = VerificationAPIIdempotencyJournalV1(tmp_path)
    journal.prepare(
        idempotency_key="idem",
        request_sha256="a" * 64,
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        trusted_root_set_sha256="e" * 64,
        authority_before=_snapshot(),
        artifact_id=None,
        artifact_sha256=None,
        bundle_id=None,
        bundle_sha256=None,
        checkpoint_v4_id=None,
        checkpoint_v4_sha256=None,
        observed_at=NOW,
    )
    first = {"result": "first"}
    journal.finalize(
        idempotency_key="idem",
        request_sha256="a" * 64,
        response_payload=first,
    )

    with pytest.raises(
        VerificationAPIIdempotencyCorruption,
        match="finalized response replay mismatch",
    ):
        journal.finalize(
            idempotency_key="idem",
            request_sha256="a" * 64,
            response_payload={"result": "different"},
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"value":1.5}',
        b'{"value":NaN}',
        b'{ "not":"canonical" }',
    ],
)
def test_idempotency_strict_json_rejects_ambiguous_input(payload: bytes) -> None:
    with pytest.raises(VerificationAPIIdempotencyCorruption):
        idempotency_module._strict_json(payload, source="test")
