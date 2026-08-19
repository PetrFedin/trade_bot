import pytest

from app.marketdata.bybit_http import (
    BybitPublicAccessBlockedError,
    BybitPublicHttpError,
    BybitPublicInvalidResponseError,
    blocked_evidence,
    decode_public_json_response,
)


def test_documented_http_403_is_classified_as_access_block() -> None:
    with pytest.raises(BybitPublicAccessBlockedError) as captured:
        decode_public_json_response(
            status_code=403,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<html> forbidden runner region </html>",
        )

    evidence = blocked_evidence(captured.value)
    assert evidence == {
        "http_status": 403,
        "content_type": "text/html",
        "body_preview": "<html> forbidden runner region </html>",
    }


def test_non_200_response_is_not_misreported_as_json_error() -> None:
    with pytest.raises(BybitPublicHttpError) as captured:
        decode_public_json_response(
            status_code=502,
            headers={"content-type": "text/plain"},
            body=b"upstream unavailable",
        )

    assert not isinstance(captured.value, BybitPublicInvalidResponseError)
    assert captured.value.diagnostic.status_code == 502


def test_invalid_json_preserves_safe_truncated_diagnostic() -> None:
    with pytest.raises(BybitPublicInvalidResponseError) as captured:
        decode_public_json_response(
            status_code=200,
            headers={"Content-Type": "text/html"},
            body=(b"not-json " * 30),
        )

    assert captured.value.diagnostic.status_code == 200
    assert captured.value.diagnostic.content_type == "text/html"
    assert len(captured.value.diagnostic.body_preview) <= 160


def test_valid_json_object_is_returned() -> None:
    payload = decode_public_json_response(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=b'{"retCode":0,"retMsg":"OK"}',
    )

    assert payload["retCode"] == 0
