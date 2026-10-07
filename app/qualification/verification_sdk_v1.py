from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Protocol, runtime_checkable

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIRequestV1,
    VerificationAPIResponseV1,
    VerificationAPIResultClass,
    encode_artifact_b64,
)

_REQUEST_PATH = "/v1/verification"
_RESPONSE_FIELDS = {
    "schema_version",
    "request_id",
    "operation",
    "request_sha256",
    "result_class",
    "usable",
    "artifact_id",
    "artifact_sha256",
    "bundle_id",
    "bundle_sha256",
    "authority_generation_before",
    "authority_record_sha256_before",
    "authority_trust_state_sha256_before",
    "authority_generation_after",
    "authority_record_sha256_after",
    "authority_trust_state_sha256_after",
    "transition_receipt",
    "failure_code",
    "failure_detail",
    "response_sha256",
}


class VerificationSDKError(RuntimeError):
    pass


class VerificationSDKProtocolError(VerificationSDKError):
    pass


class VerificationSDKTransportFailure(VerificationSDKError):
    pass


@dataclass(frozen=True)
class VerificationSDKIdempotencyKeyV1:
    value: str

    def validate(self) -> None:
        if not self.value.strip():
            raise ValueError("idempotency key cannot be blank")

    @classmethod
    def from_value(cls, value: str) -> VerificationSDKIdempotencyKeyV1:
        key = cls(value=value)
        key.validate()
        return key


@dataclass(frozen=True)
class VerificationSDKPreparedRequestV1:
    request: VerificationAPIRequestV1
    body: bytes

    def validate(self) -> None:
        self.request.validate()
        expected = canonical_json_bytes(self.request.payload())
        if self.body != expected:
            raise VerificationSDKProtocolError(
                "prepared request body does not match canonical API request"
            )

    @property
    def request_sha256(self) -> str:
        return self.request.computed_request_sha256

    @property
    def idempotency_key(self) -> str | None:
        return self.request.idempotency_key


@dataclass(frozen=True)
class VerificationSDKWireRequestV1:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class VerificationSDKTransportResponseV1:
    status: int
    headers: Mapping[str, str]
    body: bytes


@runtime_checkable
class VerificationSDKTransportV1(Protocol):
    def send(
        self,
        request: VerificationSDKWireRequestV1,
    ) -> VerificationSDKTransportResponseV1:
        ...


@dataclass(frozen=True)
class VerificationSDKResultV1:
    response: VerificationAPIResponseV1
    canonical_body: bytes
    transport_status: int

    @property
    def result_class(self) -> VerificationAPIResultClass:
        return self.response.result_class

    @property
    def usable(self) -> bool:
        return self.response.usable

    @property
    def is_conflict(self) -> bool:
        return self.response.result_class in (
            VerificationAPIResultClass.CAS_CONFLICT,
            VerificationAPIResultClass.IDEMPOTENCY_CONFLICT,
        )


def build_authority_status_request(
    *,
    request_id: str,
    authority_id: str,
    trusted_root_set_id: str,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> VerificationSDKPreparedRequestV1:
    return _prepare(
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.AUTHORITY_STATUS,
            request_id=request_id,
            authority_id=authority_id,
            trusted_root_set_id=trusted_root_set_id,
            observed_at=observed_at,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    )


def build_verify_read_only_request(
    *,
    request_id: str,
    authority_id: str,
    trusted_root_set_id: str,
    artifact_bytes: bytes,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> VerificationSDKPreparedRequestV1:
    return _prepare(
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_READ_ONLY,
            request_id=request_id,
            authority_id=authority_id,
            trusted_root_set_id=trusted_root_set_id,
            artifact_b64=encode_artifact_b64(artifact_bytes),
            observed_at=observed_at,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    )


def build_verify_advance_request(
    *,
    request_id: str,
    authority_id: str,
    trusted_root_set_id: str,
    artifact_bytes: bytes,
    idempotency_key: VerificationSDKIdempotencyKeyV1,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> VerificationSDKPreparedRequestV1:
    idempotency_key.validate()
    return _prepare(
        VerificationAPIRequestV1(
            operation=VerificationAPIOperation.VERIFY_ADVANCE,
            request_id=request_id,
            authority_id=authority_id,
            trusted_root_set_id=trusted_root_set_id,
            artifact_b64=encode_artifact_b64(artifact_bytes),
            idempotency_key=idempotency_key.value,
            observed_at=observed_at,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    )


class VerificationSDKClientV1:
    def execute(
        self,
        *,
        prepared: VerificationSDKPreparedRequestV1,
        transport: VerificationSDKTransportV1,
        max_transport_retries: int = 0,
    ) -> VerificationSDKResultV1:
        prepared.validate()
        if max_transport_retries < 0:
            raise ValueError("max_transport_retries must be non-negative")

        wire = VerificationSDKWireRequestV1(
            method="POST",
            path=_REQUEST_PATH,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(prepared.body)),
            },
            body=prepared.body,
        )

        attempts = max_transport_retries + 1
        last_failure: VerificationSDKTransportFailure | None = None
        for _ in range(attempts):
            try:
                transport_response = transport.send(wire)
            except VerificationSDKTransportFailure as exc:
                last_failure = exc
                continue

            response = decode_verification_response(
                transport_response.body,
                expected_request=prepared.request,
            )
            return VerificationSDKResultV1(
                response=response,
                canonical_body=transport_response.body,
                transport_status=transport_response.status,
            )

        if last_failure is None:
            raise VerificationSDKError(
                "transport retry loop exited without result or failure"
            )
        raise last_failure


def decode_verification_response(
    encoded: bytes,
    *,
    expected_request: VerificationAPIRequestV1 | None = None,
) -> VerificationAPIResponseV1:
    raw = _strict_canonical_json(encoded)
    actual_fields = set(raw)
    if actual_fields != _RESPONSE_FIELDS:
        raise VerificationSDKProtocolError(
            "verification response fields mismatch; "
            f"missing={sorted(_RESPONSE_FIELDS - actual_fields)}; "
            f"unknown={sorted(actual_fields - _RESPONSE_FIELDS)}"
        )

    try:
        operation = VerificationAPIOperation(
            _string(raw["operation"], "operation")
        )
        result_class = VerificationAPIResultClass(
            _string(raw["result_class"], "result_class")
        )
        transition = raw["transition_receipt"]
        if transition is not None and not isinstance(transition, dict):
            raise ValueError("transition_receipt must be an object or null")

        response = VerificationAPIResponseV1(
            schema_version=_string(raw["schema_version"], "schema_version"),
            request_id=_string(raw["request_id"], "request_id"),
            operation=operation,
            request_sha256=_string(
                raw["request_sha256"],
                "request_sha256",
            ),
            result_class=result_class,
            usable=_boolean(raw["usable"], "usable"),
            artifact_id=_optional_string(raw["artifact_id"], "artifact_id"),
            artifact_sha256=_optional_string(
                raw["artifact_sha256"],
                "artifact_sha256",
            ),
            bundle_id=_optional_string(raw["bundle_id"], "bundle_id"),
            bundle_sha256=_optional_string(
                raw["bundle_sha256"],
                "bundle_sha256",
            ),
            authority_generation_before=_optional_integer(
                raw["authority_generation_before"],
                "authority_generation_before",
            ),
            authority_record_sha256_before=_optional_string(
                raw["authority_record_sha256_before"],
                "authority_record_sha256_before",
            ),
            authority_trust_state_sha256_before=_optional_string(
                raw["authority_trust_state_sha256_before"],
                "authority_trust_state_sha256_before",
            ),
            authority_generation_after=_optional_integer(
                raw["authority_generation_after"],
                "authority_generation_after",
            ),
            authority_record_sha256_after=_optional_string(
                raw["authority_record_sha256_after"],
                "authority_record_sha256_after",
            ),
            authority_trust_state_sha256_after=_optional_string(
                raw["authority_trust_state_sha256_after"],
                "authority_trust_state_sha256_after",
            ),
            transition_receipt=(
                None if transition is None else dict(transition)
            ),
            failure_code=_optional_string(
                raw["failure_code"],
                "failure_code",
            ),
            failure_detail=_optional_string(
                raw["failure_detail"],
                "failure_detail",
            ),
            response_sha256=_string(
                raw["response_sha256"],
                "response_sha256",
            ),
        )
        response.validate()
    except (TypeError, ValueError) as exc:
        raise VerificationSDKProtocolError(
            f"invalid verification response: {exc}"
        ) from exc

    if expected_request is not None:
        _validate_response_correlation(
            response=response,
            expected_request=expected_request,
        )
    return response


def encode_verification_response(
    response: VerificationAPIResponseV1,
) -> bytes:
    return canonical_json_bytes(response.payload())


def _prepare(
    request: VerificationAPIRequestV1,
) -> VerificationSDKPreparedRequestV1:
    request.validate()
    prepared = VerificationSDKPreparedRequestV1(
        request=request,
        body=canonical_json_bytes(request.payload()),
    )
    prepared.validate()
    return prepared


def _validate_response_correlation(
    *,
    response: VerificationAPIResponseV1,
    expected_request: VerificationAPIRequestV1,
) -> None:
    if response.request_id != expected_request.request_id:
        raise VerificationSDKProtocolError("response request_id mismatch")
    if response.operation is not expected_request.operation:
        raise VerificationSDKProtocolError("response operation mismatch")
    if response.request_sha256 != expected_request.computed_request_sha256:
        raise VerificationSDKProtocolError("response request_sha256 mismatch")


def _strict_canonical_json(encoded: bytes) -> dict[str, object]:
    if not encoded:
        raise VerificationSDKProtocolError("verification response is empty")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise VerificationSDKProtocolError(
            "verification response UTF-8 BOM is forbidden"
        )
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationSDKProtocolError(
            "verification response is not valid UTF-8"
        ) from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except VerificationSDKProtocolError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VerificationSDKProtocolError(
            "verification response JSON is invalid"
        ) from exc
    if not isinstance(raw, dict):
        raise VerificationSDKProtocolError(
            "verification response root must be an object"
        )
    if canonical_json_bytes(raw) != encoded:
        raise VerificationSDKProtocolError(
            "verification response is not canonical JSON"
        )
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationSDKProtocolError(
                f"duplicate verification response JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise VerificationSDKProtocolError(
        f"floating-point verification response JSON is forbidden: {value}"
    )


def _reject_constant(value: str) -> NoReturn:
    raise VerificationSDKProtocolError(
        f"non-finite verification response JSON is forbidden: {value}"
    )


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer or null")
    return value
