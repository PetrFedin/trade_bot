from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
)
from app.qualification.verification_http_adapter_v1 import (
    create_local_http_server,
    decode_http_request,
    handle_http_request,
    _server_type_for_host,
)
from tests.test_qualification_verification_api_service_v1 import (
    _request,
    _setup,
)


def _http_payload(request) -> tuple[dict[str, str], bytes]:
    body = canonical_json_bytes(request.payload())
    return (
        {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body,
    )


def test_http_read_only_round_trip_preserves_canonical_service_response(
    tmp_path,
) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_id="http-read-1",
    )
    headers, body = _http_payload(request)
    before = authority.current()

    response = handle_http_request(
        service=service,
        method="POST",
        path="/v1/verification",
        headers=headers,
        body=body,
    )

    assert response.status == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body == canonical_json_bytes(service.handle(request))
    assert authority.current() == before


def test_http_advance_retry_after_lost_response_does_not_double_commit(
    tmp_path,
) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="http-advance-1",
        idempotency_key="http-idem-1",
    )
    headers, body = _http_payload(request)

    first = handle_http_request(
        service=service,
        method="POST",
        path="/v1/verification",
        headers=headers,
        body=body,
    )
    after_first = authority.current()

    # Simulate the client never receiving/using the first response body.
    second = handle_http_request(
        service=service,
        method="POST",
        path="/v1/verification",
        headers=headers,
        body=body,
    )
    after_second = authority.current()

    assert first.status == 200
    assert second.status == 200
    assert first.body == second.body
    assert after_first.generation == 1
    assert after_second == after_first


def test_concurrent_identical_advance_requests_commit_once(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="http-concurrent-1",
        idempotency_key="http-concurrent-idem",
    )
    headers, body = _http_payload(request)

    def call_adapter():
        return handle_http_request(
            service=service,
            method="POST",
            path="/v1/verification",
            headers=headers,
            body=body,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: call_adapter(), range(2)))

    assert responses[0].status == 200
    assert responses[1].status == 200
    assert responses[0].body == responses[1].body
    assert authority.current().generation == 1


@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "status", "error_code"),
    [
        (
            "GET",
            "/v1/verification",
            {"Content-Type": "application/json"},
            b"{}",
            405,
            "METHOD_NOT_ALLOWED",
        ),
        (
            "POST",
            "/wrong",
            {"Content-Type": "application/json"},
            b"{}",
            404,
            "NOT_FOUND",
        ),
        (
            "POST",
            "/v1/verification",
            {},
            b"{}",
            415,
            "CONTENT_TYPE_REQUIRED",
        ),
        (
            "POST",
            "/v1/verification",
            {"Content-Type": "text/plain"},
            b"{}",
            415,
            "UNSUPPORTED_CONTENT_TYPE",
        ),
        (
            "POST",
            "/v1/verification",
            {
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
            b"{}",
            400,
            "TRANSFER_ENCODING_UNSUPPORTED",
        ),
        (
            "POST",
            "/v1/verification",
            {
                "Content-Type": "application/json",
                "Content-Length": "not-int",
            },
            b"{}",
            400,
            "INVALID_CONTENT_LENGTH",
        ),
        (
            "POST",
            "/v1/verification",
            {
                "Content-Type": "application/json",
                "Content-Length": "-1",
            },
            b"{}",
            400,
            "INVALID_CONTENT_LENGTH",
        ),
        (
            "POST",
            "/v1/verification",
            {
                "Content-Type": "application/json",
                "Content-Length": "3",
            },
            b"{}",
            400,
            "CONTENT_LENGTH_MISMATCH",
        ),
    ],
)
def test_transport_rejects_invalid_http_framing_before_service(
    tmp_path,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    status: int,
    error_code: str,
) -> None:
    service, authority, _, _, _ = _setup(tmp_path)
    before = authority.current()

    response = handle_http_request(
        service=service,
        method=method,
        path=path,
        headers=headers,
        body=body,
    )

    assert response.status == status
    assert error_code.encode("utf-8") in response.body
    assert authority.current() == before


def test_transport_rejects_oversized_body_before_service(tmp_path) -> None:
    service, authority, _, _, _ = _setup(tmp_path)
    before = authority.current()
    body = b"x" * 9

    response = handle_http_request(
        service=service,
        method="POST",
        path="/v1/verification",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body=body,
        max_body_bytes=8,
    )

    assert response.status == 413
    assert b"REQUEST_TOO_LARGE" in response.body
    assert authority.current() == before


@pytest.mark.parametrize(
    ("body", "error_code"),
    [
        (b"", "EMPTY_REQUEST_BODY"),
        (b"\xef\xbb\xbf{}", "INVALID_JSON"),
        (b'{"a":1,"a":2}', "DUPLICATE_JSON_KEY"),
        (b'{"value":1.5}', "FLOATING_POINT_FORBIDDEN"),
        (b'{"value":NaN}', "NON_FINITE_JSON_FORBIDDEN"),
        (b"[]", "INVALID_JSON_ROOT"),
        (b'{ "a":1 }', "NON_CANONICAL_JSON"),
    ],
)
def test_transport_rejects_malformed_or_noncanonical_json(
    tmp_path,
    body: bytes,
    error_code: str,
) -> None:
    service, authority, _, _, _ = _setup(tmp_path)
    before = authority.current()

    response = handle_http_request(
        service=service,
        method="POST",
        path="/v1/verification",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body=body,
    )

    assert response.status == 400
    assert error_code.encode("utf-8") in response.body
    assert authority.current() == before


def test_decode_http_request_rejects_unknown_or_missing_fields(tmp_path) -> None:
    _, _, _, artifact_bytes, _ = _setup(tmp_path)
    request = _request(
        artifact_bytes,
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_id="field-check",
    )
    payload = request.payload()
    payload["unknown"] = "forbidden"
    body = canonical_json_bytes(payload)

    with pytest.raises(ValueError, match="fields mismatch"):
        decode_http_request(
            method="POST",
            path="/v1/verification",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
        )


def test_local_server_factory_rejects_non_loopback_binding(tmp_path) -> None:
    service, _, _, _, _ = _setup(tmp_path)

    with pytest.raises(ValueError, match="only to loopback"):
        create_local_http_server(
            service=service,
            host="0.0.0.0",
            port=0,
        )

    with create_local_http_server(
        service=service,
        host="127.0.0.1",
        port=0,
    ) as server:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        assert port > 0


def test_ipv6_loopback_uses_ipv6_server_class() -> None:
    server_type = _server_type_for_host("::1")

    assert server_type.address_family == socket.AF_INET6
