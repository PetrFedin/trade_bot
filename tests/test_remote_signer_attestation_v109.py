from __future__ import annotations

import base64
import json
import ssl
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app.runtime.remote_signer_attestation_v109 as v109
from app.runtime.remote_signer_attestation_v109 import (
    InMemoryRemoteSignerRepositoryV109,
    ProviderAttestationV109,
    RemoteEd25519SigningProviderV109,
    RemoteSignerConflictV109,
    RemoteSignerHttpClientV109,
    RemoteSignerHttpResponseV109,
    RemoteSignerPolicyErrorV109,
    RemoteSignerPolicySnapshotV109,
    RemoteSignerQuarantinedErrorV109,
    RemoteSignerRejectedErrorV109,
    RemoteSignerUncertainErrorV109,
    RemoteSignerValidationErrorV109,
    RemoteSignerVerificationErrorV109,
    RemoteSignRequestV109,
    VerifiedRemoteSignerPolicyV109,
    bytes_digest_v109,
    canonical_bytes_v109,
    verify_remote_sign_result_v109,
    verify_remote_signer_policy_v109,
)

UTC = UTC
NOW = datetime(2026, 8, 8, 20, 0, tzinfo=UTC)
PREDECESSOR = "1" * 64
FIRMWARE = "2" * 64
AUDIT_EVENT = "3" * 64
AUDIT_ROOT = "4" * 64


def raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def signed_policy(
    *,
    root: Ed25519PrivateKey,
    signing: Ed25519PrivateKey,
    attestation: Ed25519PrivateKey,
    generation: int = 1,
    issued_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(hours=1),
) -> RemoteSignerPolicySnapshotV109:
    common = dict(
        provider_id="provider-a",
        generation=generation,
        endpoint_origin="https://signer.example.test",
        mtls_identity_ref="workload-prod-a",
        signing_key_id="signer-key-1",
        signing_public_key_b64=b64(raw_public(signing)),
        attestation_key_id="attest-key-1",
        attestation_public_key_b64=b64(raw_public(attestation)),
        allowed_hardware_clusters=("cluster-a",),
        allowed_firmware_measurements=(FIRMWARE,),
        predecessor_keyring_digest=PREDECESSOR,
        request_ttl_seconds=60,
        timeout_seconds=2.0,
        max_response_bytes=8192,
        issued_at=issued_at,
        expires_at=expires_at,
        root_key_id="root-a",
    )
    unsigned = RemoteSignerPolicySnapshotV109(root_signature_b64=b64(b"\0" * 64), **common)
    signature = root.sign(canonical_bytes_v109(unsigned.unsigned_payload()))
    return RemoteSignerPolicySnapshotV109(root_signature_b64=b64(signature), **common)


def verified_policy() -> tuple[
    VerifiedRemoteSignerPolicyV109,
    Ed25519PrivateKey,
    Ed25519PrivateKey,
    Ed25519PrivateKey,
]:
    root = Ed25519PrivateKey.generate()
    signing = Ed25519PrivateKey.generate()
    attestation = Ed25519PrivateKey.generate()
    snapshot = signed_policy(root=root, signing=signing, attestation=attestation)
    verified = verify_remote_signer_policy_v109(
        snapshot,
        trusted_root_public_keys={"root-a": raw_public(root)},
        expected_predecessor_keyring_digest=PREDECESSOR,
        minimum_generation=0,
        observed_at=NOW,
    )
    return verified, root, signing, attestation


def request_for(
    policy: VerifiedRemoteSignerPolicyV109, payload: bytes, *, request_id: str = "req-1"
) -> RemoteSignRequestV109:
    return RemoteSignRequestV109(
        request_id=request_id,
        nonce=f"nonce-{request_id}",
        provider_id=policy.provider_id,
        policy_generation=policy.generation,
        policy_digest=policy.policy_digest,
        key_id=policy.snapshot.signing_key_id,
        key_generation=5,
        keyring_generation=9,
        purpose="CONTROLLER_COMMAND",
        domain="astra.controller.command.v109",
        payload_digest=bytes_digest_v109(payload),
        created_at=NOW,
        deadline_at=NOW + timedelta(seconds=60),
    )


def signed_attestation(
    policy: VerifiedRemoteSignerPolicyV109,
    request: RemoteSignRequestV109,
    signature: bytes,
    private: Ed25519PrivateKey,
    *,
    counter: int = 1,
    sequence: int = 1,
    cluster: str = "cluster-a",
    firmware: str = FIRMWARE,
) -> ProviderAttestationV109:
    common = dict(
        request_id=request.request_id,
        request_digest=request.request_digest,
        signature_digest=bytes_digest_v109(signature),
        provider_id=policy.provider_id,
        policy_generation=policy.generation,
        policy_digest=policy.policy_digest,
        signing_key_id=policy.snapshot.signing_key_id,
        attestation_key_id=policy.snapshot.attestation_key_id,
        hardware_cluster_id=cluster,
        firmware_measurement=firmware,
        hardware_signing_counter=counter,
        audit_sequence=sequence,
        audit_event_digest=AUDIT_EVENT,
        audit_chain_root=AUDIT_ROOT,
        attested_at=NOW + timedelta(seconds=1),
    )
    unsigned = ProviderAttestationV109(attestation_signature_b64=b64(b"\0" * 64), **common)
    signature_bytes = private.sign(canonical_bytes_v109(unsigned.unsigned_payload()))
    return ProviderAttestationV109(attestation_signature_b64=b64(signature_bytes), **common)


def response_for(
    policy: VerifiedRemoteSignerPolicyV109,
    request: RemoteSignRequestV109,
    payload: bytes,
    signing: Ed25519PrivateKey,
    attestation: Ed25519PrivateKey,
    *,
    counter: int = 1,
    sequence: int = 1,
) -> RemoteSignerHttpResponseV109:
    signature = signing.sign(payload)
    evidence = signed_attestation(
        policy,
        request,
        signature,
        attestation,
        counter=counter,
        sequence=sequence,
    )
    return RemoteSignerHttpResponseV109(
        200,
        json.dumps(
            {"signature_b64": b64(signature), "attestation": evidence.to_payload()}, default=str
        ).encode(),
    )


def test_policy_verification_and_fail_closed_boundaries() -> None:
    policy, root, signing, attestation = verified_policy()
    assert policy.policy_digest == policy.snapshot.snapshot_digest
    with pytest.raises(RemoteSignerPolicyErrorV109, match="predecessor"):
        verify_remote_signer_policy_v109(
            policy.snapshot,
            trusted_root_public_keys={"root-a": raw_public(root)},
            expected_predecessor_keyring_digest="0" * 64,
            minimum_generation=0,
            observed_at=NOW,
        )
    with pytest.raises(RemoteSignerPolicyErrorV109, match="monotonic"):
        verify_remote_signer_policy_v109(
            policy.snapshot,
            trusted_root_public_keys={"root-a": raw_public(root)},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=1,
            observed_at=NOW,
        )
    with pytest.raises(RemoteSignerPolicyErrorV109, match="untrusted"):
        verify_remote_signer_policy_v109(
            policy.snapshot,
            trusted_root_public_keys={},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=0,
            observed_at=NOW,
        )
    tampered = signed_policy(root=root, signing=signing, attestation=attestation, generation=2)
    object.__setattr__(tampered, "provider_id", "provider-b")
    with pytest.raises(RemoteSignerPolicyErrorV109, match="signature"):
        verify_remote_signer_policy_v109(
            tampered,
            trusted_root_public_keys={"root-a": raw_public(root)},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=0,
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("endpoint_origin", "http://signer.example.test", "HTTPS origin"),
        ("request_ttl_seconds", 0, "request TTL"),
        ("max_response_bytes", 10, "response size"),
        ("allowed_hardware_clusters", (), "allowlists"),
    ],
)
def test_policy_validation_rejects_unsafe_configuration(
    field: str, value: Any, message: str
) -> None:
    root = Ed25519PrivateKey.generate()
    signing = Ed25519PrivateKey.generate()
    attestation = Ed25519PrivateKey.generate()
    snapshot = signed_policy(root=root, signing=signing, attestation=attestation)
    values = {name: getattr(snapshot, name) for name in snapshot.__dataclass_fields__}
    values[field] = value
    with pytest.raises(RemoteSignerValidationErrorV109, match=message):
        RemoteSignerPolicySnapshotV109(**values)


def test_remote_result_verifies_dual_signatures_and_monotonic_checkpoint() -> None:
    policy, _, signing, attestation_private = verified_policy()
    payload = b"signed payload"
    request = request_for(policy, payload)
    signature = signing.sign(payload)
    evidence = signed_attestation(
        policy, request, signature, attestation_private, counter=7, sequence=11
    )
    result = verify_remote_sign_result_v109(
        policy=policy,
        request=request,
        payload=payload,
        signature=signature,
        attestation=evidence,
        previous_checkpoint=None,
        observed_at=NOW + timedelta(seconds=2),
    )
    assert result.signature == signature
    assert result.checkpoint.audit_sequence == 11
    previous = result.checkpoint
    repeated = signed_attestation(
        policy, request, signature, attestation_private, counter=7, sequence=12
    )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="counter"):
        verify_remote_sign_result_v109(
            policy=policy,
            request=request,
            payload=payload,
            signature=signature,
            attestation=repeated,
            previous_checkpoint=previous,
            observed_at=NOW + timedelta(seconds=3),
        )
    bad_hardware = signed_attestation(
        policy,
        request,
        signature,
        attestation_private,
        cluster="cluster-evil",
    )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="hardware cluster"):
        verify_remote_sign_result_v109(
            policy=policy,
            request=request,
            payload=payload,
            signature=signature,
            attestation=bad_hardware,
            previous_checkpoint=None,
            observed_at=NOW + timedelta(seconds=2),
        )


class FakeClient:
    def __init__(self, builder: Any) -> None:
        self.builder = builder
        self.posts = 0
        self.gets = 0
        self.last_request: RemoteSignRequestV109 | None = None

    def post_sign(
        self, request: RemoteSignRequestV109, payload: bytes
    ) -> RemoteSignerHttpResponseV109:
        self.posts += 1
        self.last_request = request
        if isinstance(self.builder, Exception):
            raise self.builder
        if callable(self.builder):
            return self.builder(request, payload)
        return self.builder

    def get_request(self, request_id: str) -> RemoteSignerHttpResponseV109:
        self.gets += 1
        if self.last_request is None or self.last_request.request_id != request_id:
            return RemoteSignerHttpResponseV109(404, b"")
        if callable(self.builder):
            return self.builder(self.last_request, b"payload")
        return self.builder


def provider_for(
    policy: VerifiedRemoteSignerPolicyV109,
    repo: InMemoryRemoteSignerRepositoryV109,
    client: FakeClient,
    *,
    clock: Any = lambda: NOW,
) -> RemoteEd25519SigningProviderV109:
    repo.install_verified_policy(policy)
    ids = iter(["req-provider-1", "req-provider-2", "req-provider-3"])
    nonces = iter(["nonce-provider-1", "nonce-provider-2", "nonce-provider-3"])
    return RemoteEd25519SigningProviderV109(
        policy=policy,
        repository=repo,
        client=client,
        worker_id="worker-a",
        key_generation=5,
        keyring_generation=9,
        purpose="CONTROLLER_COMMAND",
        domain="astra.controller.command.v109",
        clock=clock,
        request_id_factory=lambda: next(ids),
        nonce_factory=lambda: next(nonces),
    )


def test_provider_success_is_durable_and_single_post() -> None:
    policy, _, signing, attestation = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    client = FakeClient(
        lambda request, payload: response_for(policy, request, payload, signing, attestation)
    )
    provider = provider_for(policy, repo, client)
    payload = b"payload"
    assert provider.public_key_bytes() == raw_public(signing)
    assert provider.sign(payload) == signing.sign(payload)
    assert client.posts == 1
    assert client.gets == 0
    assert repo.state("req-provider-1").value == "SIGNED"


@pytest.mark.parametrize(
    ("response", "error_type", "state"),
    [
        (RemoteSignerHttpResponseV109(422, b"{}"), RemoteSignerRejectedErrorV109, "REJECTED"),
        (RemoteSignerHttpResponseV109(202, b"{}"), RemoteSignerUncertainErrorV109, "UNCERTAIN"),
        (RemoteSignerHttpResponseV109(503, b"{}"), RemoteSignerUncertainErrorV109, "UNCERTAIN"),
        (
            RemoteSignerHttpResponseV109(200, b"not-json"),
            RemoteSignerUncertainErrorV109,
            "UNCERTAIN",
        ),
    ],
)
def test_provider_never_retries_ambiguous_post(
    response: Any, error_type: type[Exception], state: str
) -> None:
    policy, _, _, _ = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    client = FakeClient(response)
    provider = provider_for(policy, repo, client)
    with pytest.raises(error_type):
        provider.sign(b"payload")
    assert client.posts == 1
    assert repo.state("req-provider-1").value == state


def test_transport_uncertainty_and_get_only_reconciliation() -> None:
    policy, _, signing, attestation = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    client = FakeClient(RemoteSignerUncertainErrorV109("lost response"))
    provider = provider_for(policy, repo, client)
    with pytest.raises(RemoteSignerUncertainErrorV109):
        provider.sign(b"payload")
    assert client.posts == 1
    assert repo.state("req-provider-1").value == "UNCERTAIN"
    client.builder = lambda request, payload: response_for(
        policy, request, b"payload", signing, attestation
    )
    assert provider.reconcile("req-provider-1", b"payload") == signing.sign(b"payload")
    assert client.posts == 1
    assert client.gets == 1
    assert repo.state("req-provider-1").value == "SIGNED"


def test_reconciliation_deadline_quarantines_without_network_get() -> None:
    policy, _, _, _ = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    client = FakeClient(RemoteSignerHttpResponseV109(202, b""))
    clocks = iter([NOW, NOW, NOW, NOW + timedelta(seconds=61)])
    provider = provider_for(policy, repo, client, clock=lambda: next(clocks))
    with pytest.raises(RemoteSignerUncertainErrorV109):
        provider.sign(b"payload")
    with pytest.raises(RemoteSignerQuarantinedErrorV109):
        provider.reconcile("req-provider-1", b"payload")
    assert client.gets == 0
    assert repo.state("req-provider-1").value == "QUARANTINED"


def test_in_memory_repository_rejects_replay_and_policy_equivocation() -> None:
    policy, _, _, _ = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    repo.install_verified_policy(policy)
    request = request_for(policy, b"payload")
    repo.create_request_with_outbox(request, b"payload")
    with pytest.raises(RemoteSignerConflictV109, match="replay"):
        repo.create_request_with_outbox(request, b"payload")
    bad = VerifiedRemoteSignerPolicyV109(policy.snapshot, policy.verified_at)
    object.__setattr__(bad.snapshot, "provider_id", "provider-a")
    object.__setattr__(bad.snapshot, "generation", policy.generation)
    object.__setattr__(bad.snapshot, "endpoint_origin", "https://equivocation.example.test")
    with pytest.raises(RemoteSignerConflictV109, match="equivocation"):
        repo.install_verified_policy(bad)


class ContextProvider:
    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context

    def ssl_context(self, identity_ref: str) -> ssl.SSLContext:
        assert identity_ref == "workload-prod-a"
        return self.context


class FakeRawResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> FakeRawResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeOpener:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[Any] = []

    def open(self, request: Any, timeout: float) -> Any:
        self.calls.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    return context


def test_http_client_enforces_tls_origin_bounds_and_no_redirect_retry() -> None:
    policy, _, _, _ = verified_policy()
    client = RemoteSignerHttpClientV109(policy, ContextProvider(tls_context()))
    fake = FakeOpener(FakeRawResponse(200, b"{}"))
    client._opener = fake  # type: ignore[attr-defined]
    response = client.get_request("req-1")
    assert response.status == 200
    assert fake.calls[0][0].get_method() == "GET"
    oversized = FakeOpener(FakeRawResponse(200, b"x" * 9000))
    client._opener = oversized  # type: ignore[attr-defined]
    with pytest.raises(RemoteSignerUncertainErrorV109, match="exceeded"):
        client.get_request("req-1")
    client._opener = FakeOpener(URLError("network"))  # type: ignore[attr-defined]
    with pytest.raises(RemoteSignerUncertainErrorV109, match="ambiguous"):
        client.get_request("req-1")


def test_http_client_returns_http_error_status_without_following_redirects() -> None:
    policy, _, _, _ = verified_policy()
    client = RemoteSignerHttpClientV109(policy, ContextProvider(tls_context()))
    error = HTTPError("https://signer.example.test/x", 409, "conflict", {}, None)
    error.read = lambda limit: b"{}"  # type: ignore[method-assign]
    client._opener = FakeOpener(error)  # type: ignore[attr-defined]
    assert client.get_request("req-1").status == 409
    bad = ssl.create_default_context()
    bad.check_hostname = False
    with pytest.raises(RemoteSignerValidationErrorV109, match="verify"):
        RemoteSignerHttpClientV109(policy, ContextProvider(bad))


def test_low_level_validation_and_canonicalization_edges() -> None:
    with pytest.raises(RemoteSignerValidationErrorV109, match="timezone-aware"):
        v109._ensure_utc(datetime(2026, 8, 8, 20, 0))
    with pytest.raises(RemoteSignerValidationErrorV109, match="invalid identifier"):
        v109._validate_id("bad id", "identifier")
    with pytest.raises(RemoteSignerValidationErrorV109, match="invalid domain"):
        v109._validate_domain("NO")
    with pytest.raises(RemoteSignerValidationErrorV109, match="invalid digest"):
        v109._validate_digest("xyz", "digest")
    with pytest.raises(RemoteSignerValidationErrorV109, match="invalid payload"):
        v109._decode_b64(123, name="payload")  # type: ignore[arg-type]
    with pytest.raises(RemoteSignerValidationErrorV109, match="invalid payload"):
        v109._decode_b64("%%", name="payload")
    with pytest.raises(RemoteSignerValidationErrorV109, match="length"):
        v109._decode_b64(b64(b"short"), expected_length=32, name="payload")
    with pytest.raises(RemoteSignerValidationErrorV109, match="bytes"):
        v109._encode_b64("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(RemoteSignerValidationErrorV109, match="payload must be bytes"):
        bytes_digest_v109("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        canonical_bytes_v109(object())
    assert canonical_bytes_v109(v109.RemoteSignStateV109.CREATED) == b'"CREATED"'


@pytest.mark.parametrize(
    "origin",
    [
        "http://signer.example.test",
        "https://user@signer.example.test",
        "https://signer.example.test/path",
        "https://signer.example.test?x=1",
        "https://signer.example.test#fragment",
    ],
)
def test_exact_origin_rejects_non_origin_urls(origin: str) -> None:
    with pytest.raises(RemoteSignerValidationErrorV109, match="exact HTTPS origin"):
        v109._validate_origin(origin)
    with pytest.raises(RemoteSignerValidationErrorV109, match="string"):
        v109._validate_origin(123)  # type: ignore[arg-type]


def test_policy_validation_and_verification_time_edges() -> None:
    root = Ed25519PrivateKey.generate()
    signing = Ed25519PrivateKey.generate()
    attestation = Ed25519PrivateKey.generate()
    baseline = signed_policy(root=root, signing=signing, attestation=attestation)
    values = {name: getattr(baseline, name) for name in baseline.__dataclass_fields__}
    invalids = [
        ("generation", 0, "generation"),
        ("timeout_seconds", 0.0, "timeout"),
        ("allowed_hardware_clusters", ("cluster-a", "cluster-a"), "duplicate hardware"),
        ("allowed_firmware_measurements", (FIRMWARE, FIRMWARE), "duplicate firmware"),
        ("expires_at", values["issued_at"], "validity interval"),
    ]
    for field, value, message in invalids:
        changed = dict(values)
        changed[field] = value
        with pytest.raises(RemoteSignerValidationErrorV109, match=message):
            RemoteSignerPolicySnapshotV109(**changed)

    future = signed_policy(
        root=root,
        signing=signing,
        attestation=attestation,
        issued_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(RemoteSignerPolicyErrorV109, match="not yet valid"):
        verify_remote_signer_policy_v109(
            future,
            trusted_root_public_keys={"root-a": raw_public(root)},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=0,
            observed_at=NOW,
            max_clock_skew_seconds=0,
        )
    expired = signed_policy(
        root=root,
        signing=signing,
        attestation=attestation,
        issued_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(RemoteSignerPolicyErrorV109, match="expired"):
        verify_remote_signer_policy_v109(
            expired,
            trusted_root_public_keys={"root-a": raw_public(root)},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=0,
            observed_at=NOW,
            max_clock_skew_seconds=0,
        )
    with pytest.raises(RemoteSignerValidationErrorV109, match="bounds"):
        verify_remote_signer_policy_v109(
            baseline,
            trusted_root_public_keys={"root-a": raw_public(root)},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=-1,
            observed_at=NOW,
        )
    with pytest.raises(RemoteSignerPolicyErrorV109, match="untrusted"):
        verify_remote_signer_policy_v109(
            baseline,
            trusted_root_public_keys={"root-a": b"short"},
            expected_predecessor_keyring_digest=PREDECESSOR,
            minimum_generation=0,
            observed_at=NOW,
        )


def test_request_attestation_and_checkpoint_validation_roundtrip() -> None:
    policy, _, signing, attestation_private = verified_policy()
    payload = b"payload"
    request = request_for(policy, payload)
    decoded = RemoteSignRequestV109.from_payload(
        json.loads(canonical_bytes_v109(request.to_payload()).decode("utf-8"))
    )
    assert decoded.request_digest == request.request_digest
    bad_payload = request.to_payload()
    bad_payload["created_at"] = 123
    with pytest.raises(RemoteSignerValidationErrorV109, match="created_at"):
        RemoteSignRequestV109.from_payload(bad_payload)
    with pytest.raises(RemoteSignerValidationErrorV109, match="generations"):
        replace(request, key_generation=0)
    with pytest.raises(RemoteSignerValidationErrorV109, match="purpose"):
        replace(request, purpose="")
    with pytest.raises(RemoteSignerValidationErrorV109, match="deadline"):
        replace(request, deadline_at=request.created_at)

    signature = signing.sign(payload)
    evidence = signed_attestation(policy, request, signature, attestation_private)
    parsed = ProviderAttestationV109.from_payload(
        json.loads(canonical_bytes_v109(evidence.to_payload()).decode("utf-8"))
    )
    assert parsed.request_digest == evidence.request_digest
    bad_attestation = evidence.to_payload()
    bad_attestation["attested_at"] = 123
    with pytest.raises(RemoteSignerValidationErrorV109, match="attested_at"):
        ProviderAttestationV109.from_payload(bad_attestation)
    with pytest.raises(RemoteSignerValidationErrorV109, match="counters"):
        replace(evidence, audit_sequence=0)
    with pytest.raises(RemoteSignerValidationErrorV109, match="audit checkpoint"):
        v109.RemoteSignerAuditCheckpointV109(
            provider_id=policy.provider_id,
            policy_generation=0,
            audit_sequence=0,
            hardware_signing_counter=0,
            audit_chain_root=AUDIT_ROOT,
            observed_at=NOW,
        )


def _verify_result(
    policy: VerifiedRemoteSignerPolicyV109,
    request: RemoteSignRequestV109,
    payload: bytes,
    signature: bytes,
    evidence: ProviderAttestationV109,
    *,
    previous: v109.RemoteSignerAuditCheckpointV109 | None = None,
    observed_at: datetime = NOW + timedelta(seconds=2),
) -> v109.VerifiedRemoteSignResultV109:
    return verify_remote_sign_result_v109(
        policy=policy,
        request=request,
        payload=payload,
        signature=signature,
        attestation=evidence,
        previous_checkpoint=previous,
        observed_at=observed_at,
    )


def test_result_verification_rejects_binding_time_and_signature_failures() -> None:
    policy, _, signing, attestation_private = verified_policy()
    payload = b"payload"
    request = request_for(policy, payload)
    signature = signing.sign(payload)
    evidence = signed_attestation(
        policy, request, signature, attestation_private, counter=10, sequence=10
    )

    with pytest.raises(RemoteSignerQuarantinedErrorV109, match="deadline"):
        _verify_result(
            policy, request, payload, signature, evidence, observed_at=request.deadline_at
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="payload digest"):
        _verify_result(policy, request, b"different", signature, evidence)
    with pytest.raises(RemoteSignerVerificationErrorV109, match="policy binding"):
        _verify_result(policy, replace(request, key_id="other-key"), payload, signature, evidence)
    with pytest.raises(RemoteSignerVerificationErrorV109, match="signature length"):
        _verify_result(policy, request, payload, b"short", evidence)
    with pytest.raises(RemoteSignerVerificationErrorV109, match="binding mismatch"):
        _verify_result(
            policy, request, payload, signature, replace(evidence, request_id="other-request")
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="firmware"):
        _verify_result(
            policy, request, payload, signature, replace(evidence, firmware_measurement="f" * 64)
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="future"):
        _verify_result(
            policy,
            request,
            payload,
            signature,
            replace(evidence, attested_at=NOW + timedelta(seconds=20)),
        )

    previous = v109.RemoteSignerAuditCheckpointV109(
        provider_id=policy.provider_id,
        policy_generation=policy.generation,
        audit_sequence=9,
        hardware_signing_counter=9,
        audit_chain_root=AUDIT_ROOT,
        observed_at=NOW,
    )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="provider mismatch"):
        _verify_result(
            policy,
            request,
            payload,
            signature,
            evidence,
            previous=replace(previous, provider_id="provider-b"),
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="generation rollback"):
        _verify_result(
            policy,
            request,
            payload,
            signature,
            evidence,
            previous=replace(previous, policy_generation=2),
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="audit sequence"):
        _verify_result(
            policy,
            request,
            payload,
            signature,
            replace(evidence, audit_sequence=9),
            previous=previous,
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="counter"):
        _verify_result(
            policy,
            request,
            payload,
            signature,
            replace(evidence, hardware_signing_counter=9),
            previous=previous,
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="signature verification"):
        _verify_result(
            policy,
            request,
            payload,
            b"x" * 64,
            replace(evidence, signature_digest=bytes_digest_v109(b"x" * 64)),
        )
    with pytest.raises(RemoteSignerVerificationErrorV109, match="signature verification"):
        _verify_result(
            policy,
            request,
            payload,
            signature,
            replace(evidence, attestation_signature_b64=b64(b"x" * 64)),
        )


def test_in_memory_repository_fail_closed_transitions() -> None:
    policy, _, signing, attestation_private = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    request = request_for(policy, b"payload", request_id="req-memory")
    with pytest.raises(RemoteSignerConflictV109, match="unverified policy"):
        repo.create_request_with_outbox(request, b"payload")
    repo.install_verified_policy(policy)
    repo.create_request_with_outbox(request, b"payload")
    repo.mark_dispatch_started(request.request_id, worker_id="worker-a", observed_at=NOW)
    with pytest.raises(RemoteSignerConflictV109, match="dispatch"):
        repo.mark_dispatch_started(request.request_id, worker_id="worker-a", observed_at=NOW)
    with pytest.raises(RemoteSignerValidationErrorV109, match="reason"):
        repo.record_rejected(request.request_id, reason="", observed_at=NOW)
    with pytest.raises(RemoteSignerValidationErrorV109, match="reason"):
        repo.record_uncertain(request.request_id, reason="", observed_at=NOW)
    with pytest.raises(RemoteSignerValidationErrorV109, match="reason"):
        repo.record_quarantined(request.request_id, reason="", observed_at=NOW)

    signature = signing.sign(b"payload")
    evidence = signed_attestation(
        policy, request, signature, attestation_private, counter=2, sequence=2
    )
    result = _verify_result(policy, request, b"payload", signature, evidence)
    repo.record_signed(request, result, observed_at=NOW + timedelta(seconds=2))
    assert repo.load_request(request.request_id).request_digest == request.request_digest
    assert repo.load_checkpoint(policy.provider_id) == result.checkpoint
    with pytest.raises(RemoteSignerConflictV109, match="terminal"):
        repo.record_uncertain(
            request.request_id, reason="late", observed_at=NOW + timedelta(seconds=3)
        )

    second = request_for(policy, b"payload-2", request_id="req-memory-2")
    repo.create_request_with_outbox(second, b"payload-2")
    repo.mark_dispatch_started(second.request_id, worker_id="worker-a", observed_at=NOW)
    second_sig = signing.sign(b"payload-2")
    second_ev = signed_attestation(
        policy, second, second_sig, attestation_private, counter=3, sequence=3
    )
    second_result = _verify_result(
        policy, second, b"payload-2", second_sig, second_ev, previous=result.checkpoint
    )
    rollback_result = replace(
        second_result,
        checkpoint=replace(second_result.checkpoint, hardware_signing_counter=2),
    )
    with pytest.raises(RemoteSignerConflictV109, match="rollback"):
        repo.record_signed(second, rollback_result, observed_at=NOW + timedelta(seconds=3))
    with pytest.raises(RemoteSignerConflictV109, match="digest conflict"):
        repo.record_signed(
            replace(second, nonce="different-nonce"),
            second_result,
            observed_at=NOW + timedelta(seconds=3),
        )


def test_http_client_rejects_weak_tls_paths_and_oversized_errors() -> None:
    policy, _, _, _ = verified_policy()
    weak = ssl.create_default_context()
    weak.minimum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises(RemoteSignerValidationErrorV109, match="TLS 1.3"):
        RemoteSignerHttpClientV109(policy, ContextProvider(weak))

    client = RemoteSignerHttpClientV109(policy, ContextProvider(tls_context()))
    with pytest.raises(RemoteSignerValidationErrorV109, match="absolute"):
        client._url("relative")  # type: ignore[attr-defined]
    error = HTTPError("https://signer.example.test/x", 503, "error", {}, None)
    error.read = lambda limit: b"x" * 9000  # type: ignore[method-assign]
    client._opener = FakeOpener(error)  # type: ignore[attr-defined]
    with pytest.raises(RemoteSignerUncertainErrorV109, match="error response exceeded"):
        client.get_request("req-1")

    fake = FakeOpener(FakeRawResponse(201, b"{}"))
    client._opener = fake  # type: ignore[attr-defined]
    request = request_for(policy, b"payload", request_id="post-1")
    assert client.post_sign(request, b"payload").status == 201
    assert fake.calls[0][0].get_method() == "POST"


def test_provider_validation_unexpected_status_and_reconcile_failures() -> None:
    policy, _, signing, attestation_private = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    with pytest.raises(RemoteSignerValidationErrorV109, match="generations"):
        RemoteEd25519SigningProviderV109(
            policy=policy,
            repository=repo,
            client=FakeClient(RemoteSignerHttpResponseV109(200, b"{}")),
            worker_id="worker-a",
            key_generation=0,
            keyring_generation=1,
            purpose="CONTROLLER_COMMAND",
            domain="astra.controller.command.v109",
            clock=lambda: NOW,
            request_id_factory=lambda: "req-x",
            nonce_factory=lambda: "nonce-x",
        )

    provider = provider_for(policy, repo, FakeClient(RemoteSignerHttpResponseV109(418, b"{}")))
    assert provider.backend.value == "KMS"
    assert provider.generation == 5
    with pytest.raises(RemoteSignerValidationErrorV109, match="non-empty"):
        provider.sign(b"")
    with pytest.raises(RemoteSignerUncertainErrorV109, match="unexpected"):
        provider.sign(b"payload")
    assert repo.state("req-provider-1").value == "UNCERTAIN"

    provider._client = FakeClient(RemoteSignerHttpResponseV109(202, b"{}"))  # type: ignore[attr-defined]
    provider._client.last_request = repo.load_request("req-provider-1")  # type: ignore[attr-defined]
    with pytest.raises(RemoteSignerUncertainErrorV109, match="incomplete"):
        provider.reconcile("req-provider-1", b"payload")

    provider._client = FakeClient(  # type: ignore[attr-defined]
        lambda request, payload: RemoteSignerHttpResponseV109(
            200, b'{"signature_b64":"bad","attestation":{}}'
        )
    )
    provider._client.last_request = repo.load_request("req-provider-1")  # type: ignore[attr-defined]
    with pytest.raises(RemoteSignerUncertainErrorV109):
        provider.reconcile("req-provider-1", b"payload")

    # Cover reconciliation verification success after an ambiguous POST on a
    # second independent repository/provider so terminal-state guards remain real.
    repo2 = InMemoryRemoteSignerRepositoryV109()
    client2 = FakeClient(RemoteSignerUncertainErrorV109("lost response"))
    provider2 = provider_for(policy, repo2, client2)
    with pytest.raises(RemoteSignerUncertainErrorV109):
        provider2.sign(b"payload")
    client2.builder = lambda request, payload: response_for(
        policy, request, b"payload", signing, attestation_private
    )
    assert provider2.reconcile("req-provider-1", b"payload") == signing.sign(b"payload")


def test_provider_success_after_deadline_is_quarantined() -> None:
    policy, _, signing, attestation_private = verified_policy()
    repo = InMemoryRemoteSignerRepositoryV109()
    client = FakeClient(
        lambda request, payload: response_for(
            policy, request, payload, signing, attestation_private
        )
    )
    clocks = iter([NOW, NOW, NOW + timedelta(seconds=61)])
    provider = provider_for(policy, repo, client, clock=lambda: next(clocks))
    with pytest.raises(RemoteSignerQuarantinedErrorV109):
        provider.sign(b"payload")
    assert repo.state("req-provider-1").value == "QUARANTINED"
