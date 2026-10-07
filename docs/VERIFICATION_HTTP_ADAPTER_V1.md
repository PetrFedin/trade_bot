# Verification HTTP Transport Adapter v1

Transport Adapter v1 exposes the already-qualified `VerificationAPIServiceV1` over a minimal stdlib HTTP boundary.

It is intentionally thin. The adapter is not a new source of trust semantics.

## Endpoint

`POST /v1/verification`

Only canonical JSON requests matching `VerificationAPIRequestV1` are accepted.

## Request framing

Required:

- `Content-Type: application/json`;
- canonical UTF-8 JSON;
- explicit body size within the configured limit;
- exact Verification API v1 request fields;
- no duplicate JSON keys;
- no floating-point or non-finite JSON values;
- no UTF-8 BOM.

The local server additionally requires `Content-Length`.

Malformed or oversized requests fail before `VerificationAPIServiceV1.handle()` is called.

## Response framing

Successful service responses are returned as canonical JSON without semantic rewriting.

The adapter only supplies transport headers and advisory HTTP status mapping.

Stable Verification API `result_class` remains authoritative.

Default advisory mapping:

- `STATUS_OK`, `VERIFIED_USABLE`, `VERIFIED_UNUSABLE` -> HTTP 200;
- `REJECTED` -> HTTP 422;
- `CAS_CONFLICT`, `IDEMPOTENCY_CONFLICT` -> HTTP 409;
- `INPUT_ERROR` -> HTTP 400;
- `AUTHORITY_ERROR` -> HTTP 503.

Transport-layer malformed-request failures use the separate
`astra-verification-http-error-v1` envelope.

## Local qualification profile

The v1 server factory accepts only loopback bindings:

- `127.0.0.1`;
- `::1`;
- `localhost`.

Binding to wildcard or routable addresses is rejected.

This keeps qualification and embedded/OEM usage local by default. A hardened network deployment profile may be introduced later without changing Verification API semantics.

## Retry and disconnect semantics

The adapter does not implement its own retry or idempotency rules.

If a client disconnects after `verify.advance` commits, retrying the same canonical request/idempotency key is handled by the underlying Verification API idempotency journal and returns the same finalized canonical response without another authority advancement.

## Concurrency

The adapter does not serialize semantic operations itself.

Concurrent requests delegate to `VerificationAPIServiceV1`, whose idempotency journal and Persistent TrustState Authority CAS remain authoritative.

## Dependency boundary

Transport Adapter v1 uses only Python stdlib HTTP primitives.

It adds no FastAPI, Flask, Starlette, Pydantic or other web-framework dependency.

It imports no broker, OMS, strategy, risk or live-routing authority.

## Qualification

The adapter is qualified through pure transport-boundary tests and local server construction.

No external network service is required by the test suite.
