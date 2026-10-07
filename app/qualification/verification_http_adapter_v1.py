from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Final

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIRequestV1,
)
from app.qualification.verification_api_service_v1 import VerificationAPIServiceV1

_HTTP_ERROR_SCHEMA: Final = "astra-verification-http-error-v1"
_REQUEST_PATH: Final = "/v1/verification"
_DEFAULT_MAX_BODY_BYTES: Final = 4 * 1024 * 1024


@dataclass(frozen=True)
class VerificationHTTPResponseV1:
    status: int
    headers: Mapping[str, str]
    body: bytes


class VerificationHTTPTransportError(ValueError):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def handle_http_request(
    *,
    service: VerificationAPIServiceV1,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
) -> VerificationHTTPResponseV1:
    try:
        request = decode_http_request(
            method=method,
            path=path,
            headers=headers,
            body=body,
            max_body_bytes=max_body_bytes,
        )
    except VerificationHTTPTransportError as exc:
        return _transport_error_response(exc)

    payload = service.handle(request)
    encoded = canonical_json_bytes(payload)
    return VerificationHTTPResponseV1(
        status=_semantic_status(payload),
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            "Cache-Control": "no-store",
        },
        body=encoded,
    )


def decode_http_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
) -> VerificationAPIRequestV1:
    if method.upper() != "POST":
        raise VerificationHTTPTransportError(
            status=405,
            code="METHOD_NOT_ALLOWED",
            detail="verification endpoint requires POST",
        )
    if path != _REQUEST_PATH:
        raise VerificationHTTPTransportError(
            status=404,
            code="NOT_FOUND",
            detail="verification endpoint not found",
        )
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")

    content_type = _header(headers, "content-type")
    if content_type is None:
        raise VerificationHTTPTransportError(
            status=415,
            code="CONTENT_TYPE_REQUIRED",
            detail="Content-Type: application/json is required",
        )
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise VerificationHTTPTransportError(
            status=415,
            code="UNSUPPORTED_CONTENT_TYPE",
            detail="only application/json is supported",
        )

    declared_length = _header(headers, "content-length")
    if declared_length is not None:
        try:
            declared = int(declared_length, 10)
        except ValueError as exc:
            raise VerificationHTTPTransportError(
                status=400,
                code="INVALID_CONTENT_LENGTH",
                detail="Content-Length must be an integer",
            ) from exc
        if declared < 0:
            raise VerificationHTTPTransportError(
                status=400,
                code="INVALID_CONTENT_LENGTH",
                detail="Content-Length must be non-negative",
            )
        if declared > max_body_bytes:
            raise VerificationHTTPTransportError(
                status=413,
                code="REQUEST_TOO_LARGE",
                detail="request body exceeds configured limit",
            )
        if declared != len(body):
            raise VerificationHTTPTransportError(
                status=400,
                code="CONTENT_LENGTH_MISMATCH",
                detail="Content-Length does not match received body",
            )

    if len(body) > max_body_bytes:
        raise VerificationHTTPTransportError(
            status=413,
            code="REQUEST_TOO_LARGE",
            detail="request body exceeds configured limit",
        )
    if not body:
        raise VerificationHTTPTransportError(
            status=400,
            code="EMPTY_REQUEST_BODY",
            detail="request body cannot be empty",
        )

    raw = _strict_canonical_json(body)
    expected_fields = {
        "schema_version",
        "operation",
        "request_id",
        "authority_id",
        "trusted_root_set_id",
        "artifact_b64",
        "idempotency_key",
        "observed_at",
        "max_clock_skew_seconds",
        "request_sha256",
    }
    actual_fields = set(raw)
    if actual_fields != expected_fields:
        raise VerificationHTTPTransportError(
            status=400,
            code="REQUEST_FIELDS_MISMATCH",
            detail=(
                f"request fields mismatch; "
                f"missing={sorted(expected_fields - actual_fields)}; "
                f"unknown={sorted(actual_fields - expected_fields)}"
            ),
        )

    try:
        operation = VerificationAPIOperation(_string(raw["operation"], "operation"))
        observed_at = datetime.fromisoformat(
            _string(raw["observed_at"], "observed_at")
        )
        max_clock_skew = _integer(
            raw["max_clock_skew_seconds"],
            "max_clock_skew_seconds",
        )
        request = VerificationAPIRequestV1(
            schema_version=_string(raw["schema_version"], "schema_version"),
            operation=operation,
            request_id=_string(raw["request_id"], "request_id"),
            authority_id=_string(raw["authority_id"], "authority_id"),
            trusted_root_set_id=_string(
                raw["trusted_root_set_id"],
                "trusted_root_set_id",
            ),
            artifact_b64=_optional_string(raw["artifact_b64"], "artifact_b64"),
            idempotency_key=_optional_string(
                raw["idempotency_key"],
                "idempotency_key",
            ),
            observed_at=observed_at,
            max_clock_skew_seconds=max_clock_skew,
            request_sha256=_optional_string(
                raw["request_sha256"],
                "request_sha256",
            ),
        )
        request.validate()
    except (TypeError, ValueError) as exc:
        raise VerificationHTTPTransportError(
            status=400,
            code="INVALID_REQUEST",
            detail=str(exc),
        ) from exc
    return request


def create_local_http_server(
    *,
    service: VerificationAPIServiceV1,
    host: str = "127.0.0.1",
    port: int = 0,
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
) -> ThreadingHTTPServer:
    if not _is_loopback(host):
        raise ValueError("Verification HTTP adapter v1 may bind only to loopback")
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")

    handler_type = _handler_factory(
        service=service,
        max_body_bytes=max_body_bytes,
    )
    server = ThreadingHTTPServer((host, port), handler_type)
    server.daemon_threads = True
    return server


def _handler_factory(
    *,
    service: VerificationAPIServiceV1,
    max_body_bytes: int,
) -> type[BaseHTTPRequestHandler]:
    class VerificationHandler(BaseHTTPRequestHandler):
        server_version = "ASTRAVerificationHTTP/1"
        sys_version = ""

        def do_POST(self) -> None:
            declared = self.headers.get("Content-Length")
            if declared is None:
                response = _transport_error_response(
                    VerificationHTTPTransportError(
                        status=411,
                        code="CONTENT_LENGTH_REQUIRED",
                        detail="Content-Length is required",
                    )
                )
                self._write_response(response)
                return
            try:
                length = int(declared, 10)
            except ValueError:
                response = _transport_error_response(
                    VerificationHTTPTransportError(
                        status=400,
                        code="INVALID_CONTENT_LENGTH",
                        detail="Content-Length must be an integer",
                    )
                )
                self._write_response(response)
                return
            if length < 0:
                response = _transport_error_response(
                    VerificationHTTPTransportError(
                        status=400,
                        code="INVALID_CONTENT_LENGTH",
                        detail="Content-Length must be non-negative",
                    )
                )
                self._write_response(response)
                return
            if length > max_body_bytes:
                response = _transport_error_response(
                    VerificationHTTPTransportError(
                        status=413,
                        code="REQUEST_TOO_LARGE",
                        detail="request body exceeds configured limit",
                    )
                )
                self._write_response(response)
                return

            body = self.rfile.read(length)
            response = handle_http_request(
                service=service,
                method="POST",
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
                max_body_bytes=max_body_bytes,
            )
            self._write_response(response)

        def do_GET(self) -> None:
            self._write_response(
                _transport_error_response(
                    VerificationHTTPTransportError(
                        status=405,
                        code="METHOD_NOT_ALLOWED",
                        detail="verification endpoint requires POST",
                    )
                )
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_response(
            self,
            response: VerificationHTTPResponseV1,
        ) -> None:
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(response.body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return VerificationHandler


def _semantic_status(payload: Mapping[str, object]) -> int:
    result_class = payload.get("result_class")
    if result_class in ("STATUS_OK", "VERIFIED_USABLE", "VERIFIED_UNUSABLE"):
        return 200
    if result_class == "REJECTED":
        return 422
    if result_class in ("CAS_CONFLICT", "IDEMPOTENCY_CONFLICT"):
        return 409
    if result_class == "INPUT_ERROR":
        return 400
    if result_class == "AUTHORITY_ERROR":
        return 503
    return 500


def _transport_error_response(
    error: VerificationHTTPTransportError,
) -> VerificationHTTPResponseV1:
    payload = {
        "schema_version": _HTTP_ERROR_SCHEMA,
        "error_code": error.code,
        "error_detail": error.detail,
    }
    encoded = canonical_json_bytes(payload)
    return VerificationHTTPResponseV1(
        status=error.status,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            "Cache-Control": "no-store",
            "Connection": "close",
        },
        body=encoded,
    )


def _strict_canonical_json(encoded: bytes) -> dict[str, object]:
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise VerificationHTTPTransportError(
            status=400,
            code="INVALID_JSON",
            detail="UTF-8 BOM is forbidden",
        )
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationHTTPTransportError(
            status=400,
            code="INVALID_JSON",
            detail="request body must be valid UTF-8",
        ) from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except VerificationHTTPTransportError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VerificationHTTPTransportError(
            status=400,
            code="INVALID_JSON",
            detail="request body must be valid JSON",
        ) from exc
    if not isinstance(raw, dict):
        raise VerificationHTTPTransportError(
            status=400,
            code="INVALID_JSON_ROOT",
            detail="request JSON root must be an object",
        )
    if canonical_json_bytes(raw) != encoded:
        raise VerificationHTTPTransportError(
            status=400,
            code="NON_CANONICAL_JSON",
            detail="request body must use canonical JSON encoding",
        )
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationHTTPTransportError(
                status=400,
                code="DUPLICATE_JSON_KEY",
                detail=f"duplicate JSON key: {key}",
            )
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise VerificationHTTPTransportError(
        status=400,
        code="FLOATING_POINT_FORBIDDEN",
        detail=f"floating-point JSON is forbidden: {value}",
    )


def _reject_constant(value: str) -> None:
    raise VerificationHTTPTransportError(
        status=400,
        code="NON_FINITE_JSON_FORBIDDEN",
        detail=f"non-finite JSON is forbidden: {value}",
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return value
    return None


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"
