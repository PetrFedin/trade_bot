from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_BODY_PREVIEW_LIMIT = 160


@dataclass(frozen=True)
class BybitPublicHttpDiagnostic:
    status_code: int
    content_type: str
    body_preview: str


class BybitPublicHttpError(RuntimeError):
    def __init__(self, message: str, *, diagnostic: BybitPublicHttpDiagnostic) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class BybitPublicAccessBlockedError(BybitPublicHttpError):
    """Known access-policy block such as the documented HTTP 403 restriction."""


class BybitPublicInvalidResponseError(BybitPublicHttpError):
    """Unexpected non-JSON or malformed public Bybit response."""


def decode_public_json_response(
    *,
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
) -> Mapping[str, Any]:
    diagnostic = BybitPublicHttpDiagnostic(
        status_code=status_code,
        content_type=_content_type(headers),
        body_preview=_safe_body_preview(body),
    )
    if status_code == 403:
        raise BybitPublicAccessBlockedError(
            "Bybit public API access blocked with HTTP 403",
            diagnostic=diagnostic,
        )
    if status_code != 200:
        raise BybitPublicHttpError(
            f"Bybit public HTTP request failed:{status_code}",
            diagnostic=diagnostic,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BybitPublicInvalidResponseError(
            "Bybit public API returned invalid JSON",
            diagnostic=diagnostic,
        ) from exc
    if not isinstance(payload, dict):
        raise BybitPublicInvalidResponseError(
            "Bybit public API response must be a JSON object",
            diagnostic=diagnostic,
        )
    return payload


def blocked_evidence(error: BybitPublicHttpError) -> dict[str, object]:
    return {
        "http_status": error.diagnostic.status_code,
        "content_type": error.diagnostic.content_type,
        "body_preview": error.diagnostic.body_preview,
    }


def _content_type(headers: Mapping[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return ""


def _safe_body_preview(body: bytes) -> str:
    text = body[:_BODY_PREVIEW_LIMIT].decode("utf-8", errors="replace")
    return " ".join(text.split())
