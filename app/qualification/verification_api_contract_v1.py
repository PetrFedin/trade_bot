from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.qualification.portable_artifact_codec import canonical_json_bytes

_REQUEST_SCHEMA = "astra-verification-api-request-v1"
_RESPONSE_SCHEMA = "astra-verification-api-response-v1"


class VerificationAPIOperation(StrEnum):
    VERIFY_READ_ONLY = "verify.read_only"
    VERIFY_ADVANCE = "verify.advance"
    AUTHORITY_STATUS = "authority.status"


class VerificationAPIResultClass(StrEnum):
    VERIFIED_USABLE = "VERIFIED_USABLE"
    VERIFIED_UNUSABLE = "VERIFIED_UNUSABLE"
    REJECTED = "REJECTED"
    CAS_CONFLICT = "CAS_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INPUT_ERROR = "INPUT_ERROR"
    AUTHORITY_ERROR = "AUTHORITY_ERROR"


@dataclass(frozen=True)
class VerificationAPIRequestV1:
    operation: VerificationAPIOperation
    request_id: str
    authority_id: str
    trusted_root_set_id: str
    observed_at: datetime
    artifact_b64: str | None = None
    idempotency_key: str | None = None
    max_clock_skew_seconds: int = 5
    request_sha256: str | None = None
    schema_version: str = _REQUEST_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _REQUEST_SCHEMA:
            raise ValueError("verification API request schema mismatch")
        for name, value in (
            ("request_id", self.request_id),
            ("authority_id", self.authority_id),
            ("trusted_root_set_id", self.trusted_root_set_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        observed = _aware(self.observed_at, "observed_at")
        if observed != self.observed_at.astimezone(UTC):
            raise ValueError("observed_at normalization failed")
        if self.max_clock_skew_seconds < 0:
            raise ValueError("max_clock_skew_seconds must be non-negative")

        if self.operation is VerificationAPIOperation.AUTHORITY_STATUS:
            if self.artifact_b64 is not None:
                raise ValueError("authority.status must not include an artifact")
            if self.idempotency_key is not None:
                raise ValueError("authority.status must not include an idempotency key")
        else:
            if self.artifact_b64 is None:
                raise ValueError("verification operations require artifact_b64")
            _decode_artifact(self.artifact_b64)
            if self.operation is VerificationAPIOperation.VERIFY_ADVANCE:
                if self.idempotency_key is None or not self.idempotency_key.strip():
                    raise ValueError("verify.advance requires idempotency_key")
            elif self.idempotency_key is not None:
                raise ValueError("verify.read_only must not include idempotency_key")

        if self.request_sha256 is not None:
            _digest(self.request_sha256, "request_sha256")
            if self.request_sha256 != self.computed_request_sha256:
                raise ValueError("verification API request digest mismatch")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation.value,
            "request_id": self.request_id,
            "authority_id": self.authority_id,
            "trusted_root_set_id": self.trusted_root_set_id,
            "artifact_b64": self.artifact_b64,
            "idempotency_key": self.idempotency_key,
            "observed_at": _aware(self.observed_at, "observed_at").isoformat(),
            "max_clock_skew_seconds": self.max_clock_skew_seconds,
        }

    @property
    def computed_request_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self.unsigned_payload(),
            "request_sha256": self.computed_request_sha256,
        }

    def artifact_bytes(self) -> bytes:
        if self.artifact_b64 is None:
            raise ValueError("request has no artifact")
        return _decode_artifact(self.artifact_b64)


@dataclass(frozen=True)
class VerificationAPIResponseV1:
    request_id: str
    operation: VerificationAPIOperation
    request_sha256: str
    result_class: VerificationAPIResultClass
    usable: bool
    artifact_id: str | None = None
    artifact_sha256: str | None = None
    bundle_id: str | None = None
    bundle_sha256: str | None = None
    authority_generation_before: int | None = None
    authority_record_sha256_before: str | None = None
    authority_trust_state_sha256_before: str | None = None
    authority_generation_after: int | None = None
    authority_record_sha256_after: str | None = None
    authority_trust_state_sha256_after: str | None = None
    transition_receipt: dict[str, object] | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    response_sha256: str | None = None
    schema_version: str = _RESPONSE_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _RESPONSE_SCHEMA:
            raise ValueError("verification API response schema mismatch")
        if not self.request_id.strip():
            raise ValueError("request_id is required")
        _digest(self.request_sha256, "request_sha256")
        for name, value in (
            ("artifact_sha256", self.artifact_sha256),
            ("bundle_sha256", self.bundle_sha256),
            ("authority_record_sha256_before", self.authority_record_sha256_before),
            (
                "authority_trust_state_sha256_before",
                self.authority_trust_state_sha256_before,
            ),
            ("authority_record_sha256_after", self.authority_record_sha256_after),
            (
                "authority_trust_state_sha256_after",
                self.authority_trust_state_sha256_after,
            ),
        ):
            if value is not None:
                _digest(value, name)
        for name, value in (
            ("authority_generation_before", self.authority_generation_before),
            ("authority_generation_after", self.authority_generation_after),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.result_class is VerificationAPIResultClass.VERIFIED_USABLE:
            if not self.usable:
                raise ValueError("VERIFIED_USABLE requires usable=true")
        elif self.usable:
            raise ValueError("only VERIFIED_USABLE may set usable=true")
        if self.response_sha256 is not None:
            _digest(self.response_sha256, "response_sha256")
            if self.response_sha256 != self.computed_response_sha256:
                raise ValueError("verification API response digest mismatch")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "request_sha256": self.request_sha256,
            "result_class": self.result_class.value,
            "usable": self.usable,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "authority_generation_before": self.authority_generation_before,
            "authority_record_sha256_before": self.authority_record_sha256_before,
            "authority_trust_state_sha256_before": (
                self.authority_trust_state_sha256_before
            ),
            "authority_generation_after": self.authority_generation_after,
            "authority_record_sha256_after": self.authority_record_sha256_after,
            "authority_trust_state_sha256_after": (
                self.authority_trust_state_sha256_after
            ),
            "transition_receipt": self.transition_receipt,
            "failure_code": self.failure_code,
            "failure_detail": self.failure_detail,
        }

    @property
    def computed_response_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self.unsigned_payload(),
            "response_sha256": self.computed_response_sha256,
        }


def encode_artifact_b64(artifact_bytes: bytes) -> str:
    if not artifact_bytes:
        raise ValueError("artifact bytes cannot be empty")
    return base64.b64encode(artifact_bytes).decode("ascii")


def _decode_artifact(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("artifact_b64 must be strict base64") from exc
    if not decoded:
        raise ValueError("artifact_b64 cannot decode to empty bytes")
    return decoded


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
