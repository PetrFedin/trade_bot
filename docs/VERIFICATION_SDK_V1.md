# Verification SDK Contract v1

Verification SDK v1 is a deterministic client contract for the canonical ASTRA Verification API.

It is not a verifier and does not make trust decisions.

## Responsibilities

The SDK provides:

- deterministic request builders;
- canonical request bytes and request SHA preservation;
- explicit idempotency-key handling for mutating verification;
- a transport abstraction;
- a loopback stdlib HTTP transport implementation;
- strict canonical response decoding;
- typed Verification API result classes;
- response/request correlation;
- bounded retry of transport failures only.

## Request builders

Supported operations:

- `authority.status`
- `verify.read_only`
- `verify.advance`

The same logical input produces byte-identical canonical request bytes and the same request SHA.

`verify.advance` requires an explicit `VerificationSDKIdempotencyKeyV1`.

The SDK never silently creates or replaces an idempotency key during retry.

## Transport contract

The SDK prepares one immutable wire request:

- `POST`
- `/v1/verification`
- `Content-Type: application/json`
- exact canonical body
- exact `Content-Length`

A transport implementation only moves these bytes.

The first concrete implementation is a stdlib HTTP transport restricted to loopback targets.

Transport status codes are advisory. Verification semantics come from the decoded `result_class`.

## Retry discipline

The client retries only `VerificationSDKTransportFailure`.

Every retry reuses the exact same:

- request bytes;
- request SHA;
- request ID;
- idempotency key.

Semantic API responses are never retried automatically, including:

- `CAS_CONFLICT`;
- `IDEMPOTENCY_CONFLICT`;
- `REJECTED`;
- `AUTHORITY_ERROR`.

A caller may choose a new operation after inspecting such a result, but the SDK does not silently re-run verification against a newer authority state.

## Response decoder

Responses must be:

- canonical UTF-8 JSON;
- exact Verification API response schema;
- free of duplicate keys;
- free of floating-point / non-finite JSON;
- valid for the declared response SHA;
- valid for the declared result class.

Unknown future result classes fail closed in v1.

## Response correlation

When decoding a response for a prepared request, the SDK requires exact equality of:

- `request_id`;
- `operation`;
- `request_sha256`.

A mismatch is a protocol error.

## Trust boundary

SDK v1 does not import or implement:

- `VerificationServiceV4`;
- signature verification;
- TrustState mutation;
- Persistent TrustState Authority;
- trusted-root interpretation;
- keyring logic;
- broker / OMS / strategy / risk / live-routing authority.

The server remains the source of verification semantics.

## Qualification

Tests cover:

- deterministic request construction;
- byte-stable request SHA;
- strict canonical response decoding;
- response correlation;
- explicit idempotency preservation;
- bounded transport retry;
- no semantic retry;
- typed CAS / IDEMPOTENCY conflicts;
- fake transport without network;
- local stdlib HTTP round trip;
- transport connection failure classification.

Only after SDK Contract v1 qualifies and merges may Reference Profiles work begin.
