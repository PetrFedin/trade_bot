from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from typing import Mapping, Protocol, Sequence
from urllib.parse import quote, urlencode, urlparse

UTC = timezone.utc
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class S3AdapterErrorV105(RuntimeError):
    pass


class S3ConfigurationErrorV105(S3AdapterErrorV105):
    pass


class S3TransportErrorV105(S3AdapterErrorV105):
    pass


class AmbiguousS3MutationV105(S3TransportErrorV105):
    pass


class S3IntegrityErrorV105(S3AdapterErrorV105):
    pass


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise S3ConfigurationErrorV105(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): ("***" if any(token in str(key).lower() for token in ("secret", "token", "authorization", "credential")) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class S3CredentialLeaseV105:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    generation: int = 1
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=15))

    def __post_init__(self) -> None:
        if not self.access_key_id or len(self.secret_access_key) < 24 or self.generation <= 0:
            raise S3ConfigurationErrorV105("invalid credential lease")
        _aware(self.expires_at, "expires_at")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.access_key_id.encode("utf-8")).hexdigest()[:16]


class CredentialProviderV105(Protocol):
    def lease(self, now: datetime) -> S3CredentialLeaseV105: ...


@dataclass(frozen=True, slots=True)
class S3ConfigV105:
    endpoint: str
    allowed_hosts: tuple[str, ...]
    bucket: str
    prefix: str
    region: str
    timeout_seconds: float = 10.0
    max_part_bytes: int = 5 * 1024 * 1024
    max_object_bytes: int = 64 * 1024 * 1024
    max_read_retries: int = 2
    sse_kms_key_id: str | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise S3ConfigurationErrorV105("endpoint must be credential-free HTTPS origin")
        if parsed.hostname not in self.allowed_hosts:
            raise S3ConfigurationErrorV105("endpoint host not allowlisted")
        if not _BUCKET_RE.fullmatch(self.bucket):
            raise S3ConfigurationErrorV105("invalid bucket")
        if self.prefix.startswith("/") or ".." in self.prefix.split("/"):
            raise S3ConfigurationErrorV105("invalid prefix")
        if self.timeout_seconds <= 0 or self.max_part_bytes <= 0 or self.max_object_bytes < self.max_part_bytes or self.max_read_retries < 0:
            raise S3ConfigurationErrorV105("invalid transport limits")
        if not self.region.strip():
            raise S3ConfigurationErrorV105("region required")


@dataclass(frozen=True, slots=True)
class S3RequestV105:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_seconds: float
    tls_verify: bool = True
    allow_redirects: bool = False


@dataclass(frozen=True, slots=True)
class S3ResponseV105:
    status_code: int
    headers: Mapping[str, str]
    body: bytes = b""


class S3TransportV105(Protocol):
    def execute(self, request: S3RequestV105) -> S3ResponseV105: ...


@dataclass(frozen=True, slots=True)
class UploadedPartV105:
    part_number: int
    etag: str
    checksum_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadResultV105:
    object_key: str
    upload_id: str
    object_sha256: str
    size_bytes: int
    parts: tuple[UploadedPartV105, ...]
    recovered_after_ambiguous_complete: bool


class S3RequestSignerV105:
    def sign(self, request: S3RequestV105, credential: S3CredentialLeaseV105, now: datetime) -> S3RequestV105:
        now = _aware(now, "now")
        if now >= credential.expires_at:
            raise S3ConfigurationErrorV105("credential lease expired")
        body_digest = _sha256(request.body)
        canonical = "\n".join((request.method, request.url, body_digest, now.strftime("%Y%m%dT%H%M%SZ"), str(credential.generation))).encode("utf-8")
        signature = hmac.new(credential.secret_access_key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        headers = dict(request.headers)
        headers.update({
            "x-amz-content-sha256": body_digest,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "Authorization": f"ASTRA-HMAC Credential={credential.access_key_id},Signature={signature}",
            "x-astra-credential-generation": str(credential.generation),
        })
        if credential.session_token:
            headers["x-amz-security-token"] = credential.session_token
        return S3RequestV105(request.method, request.url, headers, request.body, request.timeout_seconds, request.tls_verify, request.allow_redirects)


class S3EvidenceAdapterV105:
    def __init__(self, config: S3ConfigV105, transport: S3TransportV105, credentials: CredentialProviderV105, signer: S3RequestSignerV105 | None = None) -> None:
        self.config = config
        self.transport = transport
        self.credentials = credentials
        self.signer = signer or S3RequestSignerV105()
        self.audit_events: list[dict[str, object]] = []

    def _object_key(self, relative_key: str) -> str:
        if not relative_key or relative_key.startswith("/") or ".." in relative_key.split("/") or "\\" in relative_key:
            raise S3ConfigurationErrorV105("invalid object key")
        key = "/".join(part for part in (self.config.prefix.strip("/"), relative_key.strip("/")) if part)
        if len(key.encode("utf-8")) > 1024:
            raise S3ConfigurationErrorV105("object key too long")
        return key

    def _url(self, key: str, query: Sequence[tuple[str, str]] = ()) -> str:
        base = self.config.endpoint.rstrip("/")
        path = f"/{quote(self.config.bucket, safe='')}/{quote(key, safe='/')}"
        return base + path + ("?" + urlencode(query) if query else "")

    def _execute(self, method: str, key: str, *, query: Sequence[tuple[str, str]] = (), body: bytes = b"", headers: Mapping[str, str] | None = None, now: datetime) -> S3ResponseV105:
        request = S3RequestV105(method, self._url(key, query), dict(headers or {}), body, self.config.timeout_seconds, True, False)
        signed = self.signer.sign(request, self.credentials.lease(now), now)
        response = self.transport.execute(signed)
        self.audit_events.append({"method": method, "key": key, "status": response.status_code, "headers": _redact(signed.headers)})
        if 300 <= response.status_code < 400:
            raise S3TransportErrorV105("redirect rejected")
        return response

    def _read_with_retry(self, method: str, key: str, *, query: Sequence[tuple[str, str]] = (), now: datetime) -> S3ResponseV105:
        last: Exception | None = None
        for _attempt in range(self.config.max_read_retries + 1):
            try:
                response = self._execute(method, key, query=query, now=now)
                if response.status_code >= 500:
                    raise S3TransportErrorV105("read transport unavailable")
                return response
            except S3TransportErrorV105 as exc:
                last = exc
        assert last is not None
        raise last

    def upload(self, relative_key: str, payload: bytes, *, now: datetime) -> UploadResultV105:
        now = _aware(now, "now")
        if not payload or len(payload) > self.config.max_object_bytes:
            raise S3ConfigurationErrorV105("invalid object size")
        key = self._object_key(relative_key)
        object_digest = _sha256(payload)
        metadata = {"x-amz-meta-astra-sha256": object_digest, "x-amz-meta-astra-size": str(len(payload))}
        if self.config.sse_kms_key_id:
            metadata.update({
                "x-amz-server-side-encryption": "aws:kms",
                "x-amz-server-side-encryption-aws-kms-key-id": self.config.sse_kms_key_id,
            })

        try:
            start = self._execute("POST", key, query=(("uploads", ""),), headers=metadata, now=now)
        except AmbiguousS3MutationV105:
            start = self._recover_upload(key, object_digest, len(payload), now)
        if start.status_code not in (200, 201):
            raise S3TransportErrorV105("multipart start rejected")
        upload_id = start.headers.get("x-astra-upload-id")
        if not upload_id:
            raise S3IntegrityErrorV105("missing upload id")

        parts: list[UploadedPartV105] = []
        for index, offset in enumerate(range(0, len(payload), self.config.max_part_bytes), 1):
            chunk = payload[offset : offset + self.config.max_part_bytes]
            checksum = _sha256(chunk)
            response = self._execute(
                "PUT",
                key,
                query=(("partNumber", str(index)), ("uploadId", upload_id)),
                body=chunk,
                headers={"x-amz-checksum-sha256": checksum},
                now=now,
            )
            if response.status_code not in (200, 201):
                raise S3TransportErrorV105("part upload rejected")
            etag = response.headers.get("ETag", "").strip('"')
            returned_checksum = response.headers.get("x-amz-checksum-sha256")
            if not etag or returned_checksum != checksum:
                raise S3IntegrityErrorV105("part integrity mismatch")
            parts.append(UploadedPartV105(index, etag, checksum, len(chunk)))

        completion_body = json.dumps(
            {"parts": [{"part_number": part.part_number, "etag": part.etag, "checksum_sha256": part.checksum_sha256} for part in parts]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        recovered = False
        try:
            completed = self._execute("POST", key, query=(("uploadId", upload_id),), body=completion_body, headers=metadata, now=now)
            if completed.status_code not in (200, 201, 204):
                raise S3TransportErrorV105("multipart complete rejected")
        except AmbiguousS3MutationV105:
            self._verify_head(key, object_digest, len(payload), now)
            recovered = True
        else:
            self._verify_head(key, object_digest, len(payload), now)
        return UploadResultV105(key, upload_id, object_digest, len(payload), tuple(parts), recovered)

    def _recover_upload(self, key: str, digest: str, size: int, now: datetime) -> S3ResponseV105:
        response = self._read_with_retry("GET", key, query=(("uploads", ""),), now=now)
        if response.status_code != 200:
            raise AmbiguousS3MutationV105("cannot recover multipart start")
        candidates = json.loads(response.body.decode("utf-8"))
        matches = [item for item in candidates if item.get("sha256") == digest and item.get("size") == size]
        if len(matches) != 1:
            raise AmbiguousS3MutationV105("ambiguous multipart recovery")
        return S3ResponseV105(200, {"x-astra-upload-id": str(matches[0]["upload_id"])})

    def _verify_head(self, key: str, digest: str, size: int, now: datetime) -> None:
        response = self._read_with_retry("HEAD", key, now=now)
        if response.status_code != 200:
            raise S3IntegrityErrorV105("completed object not visible")
        if response.headers.get("x-amz-meta-astra-sha256") != digest or response.headers.get("x-amz-meta-astra-size") != str(size):
            raise S3IntegrityErrorV105("completed object metadata mismatch")
