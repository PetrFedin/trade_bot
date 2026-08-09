from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.runtime.s3_evidence_adapter_v105 import (
    AmbiguousS3MutationV105,
    S3ConfigV105,
    S3ConfigurationErrorV105,
    S3CredentialLeaseV105,
    S3EvidenceAdapterV105,
    S3IntegrityErrorV105,
    S3RequestSignerV105,
    S3RequestV105,
    S3ResponseV105,
    S3TransportErrorV105,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def config(**overrides):
    values = dict(
        endpoint="https://evidence.internal.example",
        allowed_hosts=("evidence.internal.example",),
        bucket="astra-evidence",
        prefix="fleet/a",
        region="eu-west-2",
        timeout_seconds=5.0,
        max_part_bytes=4,
        max_object_bytes=64,
        max_read_retries=2,
        sse_kms_key_id="kms-key",
    )
    values.update(overrides)
    return S3ConfigV105(**values)


class Provider:
    def __init__(self, lease=None):
        self.value = lease or S3CredentialLeaseV105("access", "s" * 32, "session-token", 3, NOW + timedelta(minutes=5))
        self.calls = 0

    def lease(self, now):
        self.calls += 1
        return self.value


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.mode = "normal"
        self.upload_id = "upload-1"
        self.metadata = {}
        self.read_failures = 0
        self.start_calls = 0
        self.complete_calls = 0

    def execute(self, request):
        self.requests.append(request)
        parsed = urlparse(request.url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if 300 <= getattr(self, "forced_status", 0) < 400:
            return S3ResponseV105(self.forced_status, {})
        if request.method == "POST" and "uploads" in query:
            self.start_calls += 1
            if self.mode == "ambiguous-start":
                self.mode = "recover-start"
                raise AmbiguousS3MutationV105("timeout")
            if self.mode == "start-rejected":
                return S3ResponseV105(403, {})
            if self.mode == "missing-upload-id":
                return S3ResponseV105(200, {})
            self.metadata = dict(request.headers)
            return S3ResponseV105(200, {"x-astra-upload-id": self.upload_id})
        if request.method == "GET" and "uploads" in query:
            if self.read_failures:
                self.read_failures -= 1
                return S3ResponseV105(503, {})
            if self.mode == "recover-none":
                return S3ResponseV105(200, {}, b"[]")
            if self.mode == "recover-many":
                body = json.dumps([
                    {"upload_id": "a", "sha256": self.expected_digest, "size": self.expected_size},
                    {"upload_id": "b", "sha256": self.expected_digest, "size": self.expected_size},
                ]).encode()
                return S3ResponseV105(200, {}, body)
            body = json.dumps([{"upload_id": self.upload_id, "sha256": self.expected_digest, "size": self.expected_size}]).encode()
            return S3ResponseV105(200, {}, body)
        if request.method == "PUT":
            if self.mode == "part-rejected":
                return S3ResponseV105(500, {})
            checksum = request.headers.get("x-amz-checksum-sha256", "")
            if self.mode == "bad-part-checksum":
                checksum = "0" * 64
            etag = "" if self.mode == "missing-etag" else "etag"
            return S3ResponseV105(200, {"ETag": f'"{etag}"', "x-amz-checksum-sha256": checksum})
        if request.method == "POST" and "uploadId" in query:
            self.complete_calls += 1
            self.metadata = dict(request.headers)
            if self.mode == "ambiguous-complete":
                self.mode = "normal"
                raise AmbiguousS3MutationV105("timeout")
            if self.mode == "complete-rejected":
                return S3ResponseV105(409, {})
            return S3ResponseV105(200, {})
        if request.method == "HEAD":
            if self.read_failures:
                self.read_failures -= 1
                return S3ResponseV105(503, {})
            if self.mode == "head-missing":
                return S3ResponseV105(404, {})
            digest = self.metadata.get("x-amz-meta-astra-sha256", "")
            size = self.metadata.get("x-amz-meta-astra-size", "")
            if self.mode == "bad-head":
                digest = "0" * 64
            return S3ResponseV105(200, {"x-amz-meta-astra-sha256": digest, "x-amz-meta-astra-size": size})
        raise AssertionError((request.method, request.url))


def adapter(transport=None, provider=None, **config_overrides):
    return S3EvidenceAdapterV105(config(**config_overrides), transport or FakeTransport(), provider or Provider())


@pytest.mark.parametrize("kwargs", [
    {"endpoint": "http://evidence.internal.example"},
    {"endpoint": "https://user:pass@evidence.internal.example"},
    {"endpoint": "https://other.example"},
    {"endpoint": "https://evidence.internal.example?x=1"},
    {"bucket": "Bad_Bucket"},
    {"prefix": "/root"},
    {"prefix": "a/../b"},
    {"region": ""},
    {"timeout_seconds": 0},
    {"max_part_bytes": 0},
    {"max_object_bytes": 1},
    {"max_read_retries": -1},
])
def test_config_rejects_invalid(kwargs):
    with pytest.raises(S3ConfigurationErrorV105):
        config(**kwargs)


def test_credential_lease_validation_repr_and_fingerprint():
    lease = S3CredentialLeaseV105("access", "s" * 32, "token", 1, NOW + timedelta(minutes=1))
    assert "ssss" not in repr(lease)
    assert "token" not in repr(lease)
    assert len(lease.fingerprint) == 16
    with pytest.raises(S3ConfigurationErrorV105):
        S3CredentialLeaseV105("", "short", generation=0, expires_at=NOW)
    with pytest.raises(S3ConfigurationErrorV105):
        S3CredentialLeaseV105("access", "s" * 32, expires_at=datetime(2026, 1, 1))


def test_signer_enforces_expiry_and_builds_bound_request():
    signer = S3RequestSignerV105()
    request = S3RequestV105("GET", "https://host/bucket/key", {}, b"payload", 5.0)
    lease = S3CredentialLeaseV105("access", "s" * 32, None, 2, NOW + timedelta(minutes=1))
    signed = signer.sign(request, lease, NOW)
    assert signed.tls_verify is True
    assert signed.allow_redirects is False
    assert signed.headers["Authorization"].startswith("ASTRA-HMAC")
    assert signed.headers["x-astra-credential-generation"] == "2"
    with pytest.raises(S3ConfigurationErrorV105):
        signer.sign(request, replace(lease, expires_at=NOW), NOW)


def test_object_key_validation():
    a = adapter()
    for key in ("", "/root", "a/../b", "a\\b", "x" * 1025):
        with pytest.raises(S3ConfigurationErrorV105):
            a._object_key(key)
    assert a._object_key("run/one.json") == "fleet/a/run/one.json"


def test_successful_multipart_upload_tls_kms_checksums_and_redaction():
    transport = FakeTransport()
    provider = Provider()
    a = adapter(transport, provider)
    payload = b"abcdefghij"
    result = a.upload("run/evidence.json", payload, now=NOW)
    assert result.object_sha256 == hashlib.sha256(payload).hexdigest()
    assert [part.size_bytes for part in result.parts] == [4, 4, 2]
    assert result.recovered_after_ambiguous_complete is False
    assert all(request.tls_verify and not request.allow_redirects for request in transport.requests)
    assert transport.metadata["x-amz-server-side-encryption"] == "aws:kms"
    assert provider.calls == len(transport.requests)
    assert all(event["headers"].get("Authorization") == "***" for event in a.audit_events)
    assert all("session-token" not in json.dumps(event) for event in a.audit_events)


def test_invalid_payload_sizes_and_redirect_rejected():
    a = adapter()
    with pytest.raises(S3ConfigurationErrorV105):
        a.upload("key", b"", now=NOW)
    with pytest.raises(S3ConfigurationErrorV105):
        a.upload("key", b"x" * 65, now=NOW)
    transport = FakeTransport()
    transport.forced_status = 307
    with pytest.raises(S3TransportErrorV105, match="redirect"):
        adapter(transport).upload("key", b"x", now=NOW)


def test_start_rejection_missing_id_and_no_mutation_retry():
    for mode, error in [("start-rejected", S3TransportErrorV105), ("missing-upload-id", S3IntegrityErrorV105)]:
        transport = FakeTransport()
        transport.mode = mode
        with pytest.raises(error):
            adapter(transport).upload("key", b"abc", now=NOW)
        assert transport.start_calls == 1


def test_ambiguous_start_recovers_with_read_only_listing_and_retries_reads():
    transport = FakeTransport()
    payload = b"abcdef"
    transport.mode = "ambiguous-start"
    transport.expected_digest = hashlib.sha256(payload).hexdigest()
    transport.expected_size = len(payload)
    transport.read_failures = 1
    result = adapter(transport).upload("key", payload, now=NOW)
    assert result.upload_id == transport.upload_id
    assert transport.start_calls == 1
    assert sum(request.method == "GET" for request in transport.requests) == 2


@pytest.mark.parametrize("mode", ["recover-none", "recover-many"])
def test_ambiguous_start_recovery_requires_exactly_one_match(mode):
    transport = FakeTransport()
    payload = b"abcdef"
    transport.mode = "ambiguous-start"
    transport.expected_digest = hashlib.sha256(payload).hexdigest()
    transport.expected_size = len(payload)
    original = transport.execute
    state = {"first": True}

    def execute(request):
        if state["first"] and request.method == "POST":
            state["first"] = False
            transport.mode = mode
            transport.requests.append(request)
            transport.start_calls += 1
            raise AmbiguousS3MutationV105("timeout")
        return original(request)

    transport.execute = execute
    with pytest.raises(AmbiguousS3MutationV105):
        adapter(transport).upload("key", payload, now=NOW)


@pytest.mark.parametrize("mode,error", [
    ("part-rejected", S3TransportErrorV105),
    ("bad-part-checksum", S3IntegrityErrorV105),
    ("missing-etag", S3IntegrityErrorV105),
    ("complete-rejected", S3TransportErrorV105),
    ("head-missing", S3IntegrityErrorV105),
    ("bad-head", S3IntegrityErrorV105),
])
def test_upload_integrity_and_status_failures(mode, error):
    transport = FakeTransport()
    transport.mode = mode
    with pytest.raises(error):
        adapter(transport).upload("key", b"abcdef", now=NOW)


def test_ambiguous_complete_uses_head_only_and_succeeds():
    transport = FakeTransport()
    transport.mode = "ambiguous-complete"
    result = adapter(transport).upload("key", b"abcdef", now=NOW)
    assert result.recovered_after_ambiguous_complete is True
    assert transport.complete_calls == 1
    assert sum(request.method == "HEAD" for request in transport.requests) == 1


def test_head_read_retry_exhaustion():
    transport = FakeTransport()
    transport.read_failures = 4
    with pytest.raises(S3TransportErrorV105):
        adapter(transport, max_read_retries=1).upload("key", b"abcdef", now=NOW)
    assert sum(request.method == "HEAD" for request in transport.requests) == 2


def test_no_kms_headers_when_key_not_configured():
    transport = FakeTransport()
    adapter(transport, sse_kms_key_id=None).upload("key", b"abc", now=NOW)
    assert "x-amz-server-side-encryption" not in transport.metadata
