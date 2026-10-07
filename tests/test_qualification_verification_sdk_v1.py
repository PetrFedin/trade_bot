from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIResponseV1,
    VerificationAPIResultClass,
)
from app.qualification.verification_sdk_v1 import (
    VerificationSDKClientV1,
    VerificationSDKIdempotencyKeyV1,
    VerificationSDKPreparedRequestV1,
    VerificationSDKProtocolError,
    VerificationSDKTransportFailure,
    VerificationSDKTransportResponseV1,
    build_authority_status_request,
    build_verify_advance_request,
    build_verify_read_only_request,
    decode_verification_response,
    encode_verification_response,
)

NOW = datetime(2026, 10, 7, 15, 30, tzinfo=UTC)
ARTIFACT = b'{"artifact":"portable"}'


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("fake transport has no configured outcome")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _success_response(prepared, *, result_class=None, usable=True):
    if result_class is None:
        result_class = VerificationAPIResultClass.VERIFIED_USABLE
    response = VerificationAPIResponseV1(
        request_id=prepared.request.request_id,
        operation=prepared.request.operation,
        request_sha256=prepared.request_sha256,
        result_class=result_class,
        usable=usable,
    )
    body = encode_verification_response(response)
    return VerificationSDKTransportResponseV1(
        status=200,
        headers={"Content-Type": "application/json"},
        body=body,
    )


def test_same_logical_request_builds_byte_identical_payload_and_digest() -> None:
    first = build_verify_read_only_request(
        request_id="sdk-read-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        observed_at=NOW,
    )
    second = build_verify_read_only_request(
        request_id="sdk-read-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        observed_at=NOW,
    )

    assert first.body == second.body
    assert first.request_sha256 == second.request_sha256
    assert first.body == canonical_json_bytes(first.request.payload())


def test_all_sdk_builders_preserve_api_operation_contract() -> None:
    status = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )
    read_only = build_verify_read_only_request(
        request_id="sdk-read-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        observed_at=NOW,
    )
    advance = build_verify_advance_request(
        request_id="sdk-advance-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        idempotency_key=VerificationSDKIdempotencyKeyV1.from_value("idem-1"),
        observed_at=NOW,
    )

    assert status.request.operation is VerificationAPIOperation.AUTHORITY_STATUS
    assert status.idempotency_key is None
    assert read_only.request.operation is VerificationAPIOperation.VERIFY_READ_ONLY
    assert read_only.idempotency_key is None
    assert advance.request.operation is VerificationAPIOperation.VERIFY_ADVANCE
    assert advance.idempotency_key == "idem-1"


def test_sdk_idempotency_key_rejects_blank_values() -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        VerificationSDKIdempotencyKeyV1.from_value("   ")


def test_prepared_request_detects_body_tampering() -> None:
    prepared = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )
    tampered = VerificationSDKPreparedRequestV1(
        request=prepared.request,
        body=prepared.body + b" ",
    )

    with pytest.raises(VerificationSDKProtocolError, match="does not match"):
        tampered.validate()


def test_response_round_trip_is_canonical_and_typed() -> None:
    prepared = build_verify_read_only_request(
        request_id="sdk-read-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        observed_at=NOW,
    )
    response = VerificationAPIResponseV1(
        request_id=prepared.request.request_id,
        operation=prepared.request.operation,
        request_sha256=prepared.request_sha256,
        result_class=VerificationAPIResultClass.REJECTED,
        usable=False,
        failure_code="SIGNATURE_INVALID",
        failure_detail="rejected",
    )
    encoded = encode_verification_response(response)

    decoded = decode_verification_response(
        encoded,
        expected_request=prepared.request,
    )

    assert decoded == response
    assert encode_verification_response(decoded) == encoded


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("request_id", "other-request", "request_id mismatch"),
        (
            "operation",
            VerificationAPIOperation.AUTHORITY_STATUS,
            "operation mismatch",
        ),
        ("request_sha256", "f" * 64, "request_sha256 mismatch"),
    ],
)
def test_response_correlation_fails_closed(field, value, message) -> None:
    prepared = build_verify_read_only_request(
        request_id="sdk-read-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        observed_at=NOW,
    )
    response = VerificationAPIResponseV1(
        request_id=prepared.request.request_id,
        operation=prepared.request.operation,
        request_sha256=prepared.request_sha256,
        result_class=VerificationAPIResultClass.REJECTED,
        usable=False,
    )
    mismatched = replace(response, **{field: value})
    encoded = encode_verification_response(mismatched)

    with pytest.raises(VerificationSDKProtocolError, match=message):
        decode_verification_response(
            encoded,
            expected_request=prepared.request,
        )


@pytest.mark.parametrize(
    "encoded",
    [
        b"",
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"value":1.5}',
        b'{"value":NaN}',
        b"[]",
        b'{ "not":"canonical" }',
        b"{",
        b"\xff",
    ],
)
def test_response_decoder_rejects_noncanonical_or_ambiguous_json(encoded) -> None:
    with pytest.raises(VerificationSDKProtocolError):
        decode_verification_response(encoded)


def test_response_decoder_rejects_unknown_fields() -> None:
    prepared = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )
    response = VerificationAPIResponseV1(
        request_id=prepared.request.request_id,
        operation=prepared.request.operation,
        request_sha256=prepared.request_sha256,
        result_class=VerificationAPIResultClass.STATUS_OK,
        usable=False,
    )
    payload = response.payload()
    payload["unknown"] = "forbidden"

    with pytest.raises(VerificationSDKProtocolError, match="fields mismatch"):
        decode_verification_response(canonical_json_bytes(payload))


def test_response_decoder_rejects_unknown_future_result_class() -> None:
    prepared = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )
    response = VerificationAPIResponseV1(
        request_id=prepared.request.request_id,
        operation=prepared.request.operation,
        request_sha256=prepared.request_sha256,
        result_class=VerificationAPIResultClass.STATUS_OK,
        usable=False,
    )
    payload = response.payload()
    payload["result_class"] = "FUTURE_RESULT_CLASS"

    with pytest.raises(VerificationSDKProtocolError, match="invalid verification response"):
        decode_verification_response(canonical_json_bytes(payload))


def test_response_decoder_rejects_wrong_response_digest() -> None:
    prepared = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )
    response = VerificationAPIResponseV1(
        request_id=prepared.request.request_id,
        operation=prepared.request.operation,
        request_sha256=prepared.request_sha256,
        result_class=VerificationAPIResultClass.STATUS_OK,
        usable=False,
    )
    payload = response.payload()
    payload["response_sha256"] = "f" * 64

    with pytest.raises(VerificationSDKProtocolError, match="response digest mismatch"):
        decode_verification_response(canonical_json_bytes(payload))


def test_transport_retry_reuses_exact_request_bytes_and_idempotency_key() -> None:
    prepared = build_verify_advance_request(
        request_id="sdk-advance-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        idempotency_key=VerificationSDKIdempotencyKeyV1.from_value("idem-stable"),
        observed_at=NOW,
    )
    transport = FakeTransport(
        [
            VerificationSDKTransportFailure("disconnect-1"),
            VerificationSDKTransportFailure("disconnect-2"),
            _success_response(prepared),
        ]
    )

    result = VerificationSDKClientV1().execute(
        prepared=prepared,
        transport=transport,
        max_transport_retries=2,
    )

    assert result.result_class is VerificationAPIResultClass.VERIFIED_USABLE
    assert len(transport.requests) == 3
    assert all(item.body == prepared.body for item in transport.requests)
    assert all(item.path == "/v1/verification" for item in transport.requests)
    assert prepared.idempotency_key == "idem-stable"
    assert len({item.body for item in transport.requests}) == 1


def test_transport_retry_exhaustion_raises_last_transport_failure() -> None:
    prepared = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )
    transport = FakeTransport(
        [
            VerificationSDKTransportFailure("first"),
            VerificationSDKTransportFailure("last"),
        ]
    )

    with pytest.raises(VerificationSDKTransportFailure, match="last"):
        VerificationSDKClientV1().execute(
            prepared=prepared,
            transport=transport,
            max_transport_retries=1,
        )

    assert len(transport.requests) == 2


def test_negative_transport_retry_count_is_rejected() -> None:
    prepared = build_authority_status_request(
        request_id="sdk-status-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        observed_at=NOW,
    )

    with pytest.raises(ValueError, match="must be non-negative"):
        VerificationSDKClientV1().execute(
            prepared=prepared,
            transport=FakeTransport([]),
            max_transport_retries=-1,
        )


@pytest.mark.parametrize(
    "result_class",
    [
        VerificationAPIResultClass.CAS_CONFLICT,
        VerificationAPIResultClass.IDEMPOTENCY_CONFLICT,
        VerificationAPIResultClass.REJECTED,
        VerificationAPIResultClass.AUTHORITY_ERROR,
    ],
)
def test_semantic_results_are_never_retried(result_class) -> None:
    prepared = build_verify_advance_request(
        request_id="sdk-advance-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        idempotency_key=VerificationSDKIdempotencyKeyV1.from_value("idem-semantic"),
        observed_at=NOW,
    )
    transport = FakeTransport(
        [
            _success_response(
                prepared,
                result_class=result_class,
                usable=False,
            ),
            VerificationSDKTransportFailure("must not be reached"),
        ]
    )

    result = VerificationSDKClientV1().execute(
        prepared=prepared,
        transport=transport,
        max_transport_retries=5,
    )

    assert result.result_class is result_class
    assert len(transport.requests) == 1
    assert result.is_conflict is (
        result_class
        in (
            VerificationAPIResultClass.CAS_CONFLICT,
            VerificationAPIResultClass.IDEMPOTENCY_CONFLICT,
        )
    )


def test_transport_status_is_advisory_not_semantic() -> None:
    prepared = build_verify_advance_request(
        request_id="sdk-advance-1",
        authority_id="primary",
        trusted_root_set_id="roots-v1",
        artifact_bytes=ARTIFACT,
        idempotency_key=VerificationSDKIdempotencyKeyV1.from_value("idem-cas"),
        observed_at=NOW,
    )
    response = _success_response(
        prepared,
        result_class=VerificationAPIResultClass.CAS_CONFLICT,
        usable=False,
    )
    response = replace(response, status=503)
    transport = FakeTransport([response])

    result = VerificationSDKClientV1().execute(
        prepared=prepared,
        transport=transport,
        max_transport_retries=3,
    )

    assert result.result_class is VerificationAPIResultClass.CAS_CONFLICT
    assert result.transport_status == 503
    assert len(transport.requests) == 1
